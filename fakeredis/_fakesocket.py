import functools
import hashlib
import itertools
import math
import pickle
import random
import time

import redis

from . import _msgs as msgs
from ._basefakesocket import BaseFakeSocket
from ._commands import (
    Key, command, DbIndex, Int, CommandItem, BeforeAny, SortFloat, Float, BitOffset, BitValue, Hash,
    StringTest, ScoreTest, Timeout)
from ._helpers import (
    PONG, OK, MAX_STRING_SIZE, SimpleError, SimpleString, casematch,
    BGSAVE_STARTED, REDIS_LOG_LEVELS_TO_LOGGING, LOGGER, REDIS_LOG_LEVELS, casenorm, compile_pattern)
from ._msgs import LUA_COMMAND_ARG_MSG, LUA_COMMAND_ARG_MSG6
from ._zset import ZSet


class FakeSocket(BaseFakeSocket):
    _connection_error_class = redis.ConnectionError

    def __init__(self, server):
        super().__init__(server)

    # Connection commands
    # TODO: auth, quit

    @command((bytes,))
    def echo(self, message):
        return message

    @command((), (bytes,))
    def ping(self, *args):
        if len(args) > 1:
            raise SimpleError(msgs.WRONG_ARGS_MSG.format('ping'))
        if self._pubsub:
            return [b'pong', args[0] if args else b'']
        else:
            return args[0] if args else PONG

    @command((DbIndex,))
    def select(self, index):
        self._db = self._server.dbs[index]
        self._db_num = index
        return OK

    @command((DbIndex, DbIndex))
    def swapdb(self, index1, index2):
        if index1 != index2:
            db1 = self._server.dbs[index1]
            db2 = self._server.dbs[index2]
            db1.swap(db2)
        return OK

    # Key commands
    # TODO: lots

    @command((Key(),), (Key(),), name='del')
    def del_(self, *keys):
        return self._delete(*keys)

    @command((Key(),), (Key(),), name='unlink')
    def unlink(self, *keys):
        return self._delete(*keys)

    @command((Key(),), (Key(),))
    def exists(self, *keys):
        ret = 0
        for key in keys:
            if key:
                ret += 1
        return ret

    def _ttl(self, key, scale):
        if not key:
            return -2
        elif key.expireat is None:
            return -1
        else:
            return int(round((key.expireat - self._db.time) * scale))

    @command((Key(), Int,), (bytes,), name='expire')
    def expire(self, key, seconds, *args):
        res = self._expireat(key, self._db.time + seconds, *args)
        return res

    @command((Key(), Int))
    def expireat(self, key, timestamp):
        return self._expireat(key, float(timestamp))

    @command((Key(), Int))
    def pexpire(self, key, ms):
        return self._expireat(key, self._db.time + ms / 1000.0)

    @command((Key(), Int))
    def pexpireat(self, key, ms_timestamp):
        return self._expireat(key, ms_timestamp / 1000.0)

    @command((Key(),))
    def ttl(self, key):
        return self._ttl(key, 1.0)

    @command((Key(),))
    def pttl(self, key):
        return self._ttl(key, 1000.0)

    @command((Key(),))
    def type(self, key):
        return self._type(key)

    @command((Key(),))
    def persist(self, key):
        if key.expireat is None:
            return 0
        key.expireat = None
        return 1

    @command((bytes,))
    def keys(self, pattern):
        if pattern == b'*':
            return list(self._db)
        else:
            regex = compile_pattern(pattern)
            return [key for key in self._db if regex.match(key)]

    @command((Key(), DbIndex))
    def move(self, key, db):
        if db == self._db_num:
            raise SimpleError(msgs.SRC_DST_SAME_MSG)
        if not key or key.key in self._server.dbs[db]:
            return 0
        # TODO: what is the interaction with expiry?
        self._server.dbs[db][key.key] = self._server.dbs[self._db_num][key.key]
        key.value = None  # Causes deletion
        return 1

    @command(())
    def randomkey(self):
        keys = list(self._db.keys())
        if not keys:
            return None
        return random.choice(keys)

    @command((Key(), Key()))
    def rename(self, key, newkey):
        if not key:
            raise SimpleError(msgs.NO_KEY_MSG)
        # TODO: check interaction with WATCH
        if newkey.key != key.key:
            newkey.value = key.value
            newkey.expireat = key.expireat
            key.value = None
        return OK

    @command((Key(), Key()))
    def renamenx(self, key, newkey):
        if not key:
            raise SimpleError(msgs.NO_KEY_MSG)
        if newkey:
            return 0
        self.rename(key, newkey)
        return 1

    @command((Int,), (bytes, bytes))
    def scan(self, cursor, *args):
        return self._scan(list(self._db), cursor, *args)

    def _lookup_key(self, key, pattern):
        """Python implementation of lookupKeyByPattern from redis"""
        if pattern == b'#':
            return key
        p = pattern.find(b'*')
        if p == -1:
            return None
        prefix = pattern[:p]
        suffix = pattern[p + 1:]
        arrow = suffix.find(b'->', 0, -1)
        if arrow != -1:
            field = suffix[arrow + 2:]
            suffix = suffix[:arrow]
        else:
            field = None
        new_key = prefix + key + suffix
        item = CommandItem(new_key, self._db, item=self._db.get(new_key))
        if item.value is None:
            return None
        if field is not None:
            if not isinstance(item.value, dict):
                return None
            return item.value.get(field)
        else:
            if not isinstance(item.value, bytes):
                return None
            return item.value

    @command((Key(),), (bytes,))
    def sort(self, key, *args):
        i = 0
        desc = False
        alpha = False
        limit_start = 0
        limit_count = -1
        store = None
        sortby = None
        dontsort = False
        get = []
        if key.value is not None:
            if not isinstance(key.value, (set, list, ZSet)):
                raise SimpleError(msgs.WRONGTYPE_MSG)

        while i < len(args):
            arg = args[i]
            if casematch(arg, b'asc'):
                desc = False
            elif casematch(arg, b'desc'):
                desc = True
            elif casematch(arg, b'alpha'):
                alpha = True
            elif casematch(arg, b'limit') and i + 2 < len(args):
                try:
                    limit_start = Int.decode(args[i + 1])
                    limit_count = Int.decode(args[i + 2])
                except SimpleError:
                    raise SimpleError(msgs.SYNTAX_ERROR_MSG)
                else:
                    i += 2
            elif casematch(arg, b'store') and i + 1 < len(args):
                store = args[i + 1]
                i += 1
            elif casematch(arg, b'by') and i + 1 < len(args):
                sortby = args[i + 1]
                if b'*' not in sortby:
                    dontsort = True
                i += 1
            elif casematch(arg, b'get') and i + 1 < len(args):
                get.append(args[i + 1])
                i += 1
            else:
                raise SimpleError(msgs.SYNTAX_ERROR_MSG)
            i += 1

        # TODO: force sorting if the object is a set and either in Lua or
        # storing to a key, to match redis behaviour.
        items = list(key.value) if key.value is not None else []

        # These transformations are based on the redis implementation, but
        # changed to produce a half-open range.
        start = max(limit_start, 0)
        end = len(items) if limit_count < 0 else start + limit_count
        if start >= len(items):
            start = end = len(items) - 1
        end = min(end, len(items))

        if not get:
            get.append(b'#')
        if sortby is None:
            sortby = b'#'

        if not dontsort:
            if alpha:
                def sort_key(v):
                    byval = self._lookup_key(v, sortby)
                    # TODO: use locale.strxfrm when not storing? But then need
                    # to decode too.
                    if byval is None:
                        byval = BeforeAny()
                    return byval

            else:
                def sort_key(v):
                    byval = self._lookup_key(v, sortby)
                    score = SortFloat.decode(byval, ) if byval is not None else 0.0
                    return (score, v)

            items.sort(key=sort_key, reverse=desc)
        elif isinstance(key.value, (list, ZSet)):
            items.reverse()

        out = []
        for row in items[start:end]:
            for g in get:
                v = self._lookup_key(row, g)
                if store is not None and v is None:
                    v = b''
                out.append(v)
        if store is not None:
            item = CommandItem(store, self._db, item=self._db.get(store))
            item.value = out
            item.writeback()
            return len(out)
        else:
            return out

    @command((Key(missing_return=None),))
    def dump(self, key):
        value = pickle.dumps(key.value)
        checksum = hashlib.sha1(value).digest()
        return checksum + value

    @command((Key(), Int, bytes), (bytes,))
    def restore(self, key, ttl, value, *args):
        replace = False
        i = 0
        while i < len(args):
            if casematch(args[i], b'replace'):
                replace = True
                i += 1
            else:
                raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        if key and not replace:
            raise SimpleError(msgs.RESTORE_KEY_EXISTS)
        checksum, value = value[:20], value[20:]
        if hashlib.sha1(value).digest() != checksum:
            raise SimpleError(msgs.RESTORE_INVALID_CHECKSUM_MSG)
        if ttl < 0:
            raise SimpleError(msgs.RESTORE_INVALID_TTL_MSG)
        if ttl == 0:
            expireat = None
        else:
            expireat = self._db.time + ttl / 1000.0
        key.value = pickle.loads(value)
        key.expireat = expireat
        return OK

    # Transaction commands

    @command((), flags='s')
    def multi(self):
        if self._transaction is not None:
            raise SimpleError(msgs.MULTI_NESTED_MSG)
        self._transaction = []
        self._transaction_failed = False
        return OK

    @command((), flags='s')
    def discard(self):
        if self._transaction is None:
            raise SimpleError(msgs.WITHOUT_MULTI_MSG.format('DISCARD'))
        self._transaction = None
        self._transaction_failed = False
        self._clear_watches()
        return OK

    @command((), name='exec', flags='s')
    def exec_(self):
        if self._transaction is None:
            raise SimpleError(msgs.WITHOUT_MULTI_MSG.format('EXEC'))
        if self._transaction_failed:
            self._transaction = None
            self._clear_watches()
            raise SimpleError(msgs.EXECABORT_MSG)
        transaction = self._transaction
        self._transaction = None
        self._transaction_failed = False
        watch_notified = self._watch_notified
        self._clear_watches()
        if watch_notified:
            return None
        result = []
        for func, sig, args in transaction:
            try:
                self._in_transaction = True
                ans = self._run_command(func, sig, args, False)
            except SimpleError as exc:
                ans = exc
            finally:
                self._in_transaction = False
            result.append(ans)
        return result

    @command((Key(),), (Key(),), flags='s')
    def watch(self, *keys):
        if self._transaction is not None:
            raise SimpleError(msgs.WATCH_INSIDE_MULTI_MSG)
        for key in keys:
            if key not in self._watches:
                self._watches.add((key.key, self._db))
                self._db.add_watch(key.key, self)
        return OK

    @command((), flags='s')
    def unwatch(self):
        self._clear_watches()
        return OK

    # String commands
    # TODO: bitfield, bitop, bitpos

    @command((Key(bytes), bytes))
    def append(self, key, value):
        old = key.get(b'')
        if len(old) + len(value) > MAX_STRING_SIZE:
            raise SimpleError(msgs.STRING_OVERFLOW_MSG)
        key.update(key.get(b'') + value)
        return len(key.value)

    @command((Key(bytes, 0),), (bytes,))
    def bitcount(self, key, *args):
        # Redis checks the argument count before decoding integers. That's why
        # we can't declare them as Int.
        if args:
            if len(args) != 2:
                raise SimpleError(msgs.SYNTAX_ERROR_MSG)
            start = Int.decode(args[0])
            end = Int.decode(args[1])
            start, end = self._fix_range_string(start, end, len(key.value))
            value = key.value[start:end]
        else:
            value = key.value
        return bin(int.from_bytes(value, 'little')).count('1')

    @command((Key(bytes), Int))
    def decrby(self, key, amount):
        return self.incrby(key, -amount)

    @command((Key(bytes),))
    def decr(self, key):
        return self.incrby(key, -1)

    @command((Key(bytes), Int))
    def incrby(self, key, amount):
        c = Int.decode(key.get(b'0')) + amount
        key.update(self._encodeint(c))
        return c

    @command((Key(bytes),))
    def incr(self, key):
        return self.incrby(key, 1)

    @command((Key(bytes), bytes))
    def incrbyfloat(self, key, amount):
        # TODO: introduce convert_order so that we can specify amount is Float
        c = Float.decode(key.get(b'0')) + Float.decode(amount)
        if not math.isfinite(c):
            raise SimpleError(msgs.NONFINITE_MSG)
        encoded = self._encodefloat(c, True)
        key.update(encoded)
        return encoded

    @command((Key(bytes),))
    def get(self, key):
        return key.get(None)

    @command((Key(bytes), BitOffset))
    def getbit(self, key, offset):
        value = key.get(b'')
        byte = offset // 8
        remaining = offset % 8
        actual_bitoffset = 7 - remaining
        try:
            actual_val = value[byte]
        except IndexError:
            return 0
        return 1 if (1 << actual_bitoffset) & actual_val else 0

    @command((Key(bytes), BitOffset, BitValue))
    def setbit(self, key, offset, value):
        val = key.get(b'\x00')
        byte = offset // 8
        remaining = offset % 8
        actual_bitoffset = 7 - remaining
        if len(val) - 1 < byte:
            # We need to expand val so that we can set the appropriate
            # bit.
            needed = byte - (len(val) - 1)
            val += b'\x00' * needed
        old_byte = val[byte]
        if value == 1:
            new_byte = old_byte | (1 << actual_bitoffset)
        else:
            new_byte = old_byte & ~(1 << actual_bitoffset)
        old_value = value if old_byte == new_byte else 1 - value
        reconstructed = bytearray(val)
        reconstructed[byte] = new_byte
        key.update(bytes(reconstructed))
        return old_value

    @command((Key(bytes), Int, Int))
    def getrange(self, key, start, end):
        value = key.get(b'')
        start, end = self._fix_range_string(start, end, len(value))
        return value[start:end]

    # substr is a deprecated alias for getrange
    @command((Key(bytes), Int, Int))
    def substr(self, key, start, end):
        return self.getrange(key, start, end)

    @command((Key(bytes), bytes))
    def getset(self, key, value):
        old = key.value
        key.value = value
        return old

    @command((Key(),), (Key(),))
    def mget(self, *keys):
        return [key.value if isinstance(key.value, bytes) else None for key in keys]

    @command((Key(), bytes), (Key(), bytes))
    def mset(self, *args):
        for i in range(0, len(args), 2):
            args[i].value = args[i + 1]
        return OK

    @command((Key(), bytes), (Key(), bytes))
    def msetnx(self, *args):
        for i in range(0, len(args), 2):
            if args[i]:
                return 0
        for i in range(0, len(args), 2):
            args[i].value = args[i + 1]
        return 1

    @command((Key(), bytes), (bytes,), name='set')
    def set_(self, key, value, *args):
        i = 0
        ex = None
        px = None
        xx = False
        nx = False
        keepttl = False
        get = False
        while i < len(args):
            if casematch(args[i], b'nx'):
                nx = True
                i += 1
            elif casematch(args[i], b'xx'):
                xx = True
                i += 1
            elif casematch(args[i], b'ex') and i + 1 < len(args):
                ex = Int.decode(args[i + 1])
                if ex <= 0 or (self._db.time + ex) * 1000 >= 2 ** 63:
                    raise SimpleError(msgs.INVALID_EXPIRE_MSG.format('set'))
                i += 2
            elif casematch(args[i], b'px') and i + 1 < len(args):
                px = Int.decode(args[i + 1])
                if px <= 0 or self._db.time * 1000 + px >= 2 ** 63:
                    raise SimpleError(msgs.INVALID_EXPIRE_MSG.format('set'))
                i += 2
            elif casematch(args[i], b'keepttl'):
                keepttl = True
                i += 1
            elif casematch(args[i], b'get'):
                get = True
                i += 1
            else:
                raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        if (xx and nx) or ((px is not None) + (ex is not None) + keepttl > 1):
            raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        if nx and get and self.version < 7:
            # The command docs say this is allowed from Redis 7.0.
            raise SimpleError(msgs.SYNTAX_ERROR_MSG)

        old_value = None
        if get:
            if key.value is not None and type(key.value) is not bytes:
                raise SimpleError(msgs.WRONGTYPE_MSG)
            old_value = key.value

        if nx and key:
            return old_value
        if xx and not key:
            return old_value
        if not keepttl:
            key.value = value
        else:
            key.update(value)
        if ex is not None:
            key.expireat = self._db.time + ex
        if px is not None:
            key.expireat = self._db.time + px / 1000.0
        return OK if not get else old_value

    @command((Key(), Int, bytes))
    def setex(self, key, seconds, value):
        if seconds <= 0 or (self._db.time + seconds) * 1000 >= 2 ** 63:
            raise SimpleError(msgs.INVALID_EXPIRE_MSG.format('setex'))
        key.value = value
        key.expireat = self._db.time + seconds
        return OK

    @command((Key(), Int, bytes))
    def psetex(self, key, ms, value):
        if ms <= 0 or self._db.time * 1000 + ms >= 2 ** 63:
            raise SimpleError(msgs.INVALID_EXPIRE_MSG.format('psetex'))
        key.value = value
        key.expireat = self._db.time + ms / 1000.0
        return OK

    @command((Key(), bytes))
    def setnx(self, key, value):
        if key:
            return 0
        key.value = value
        return 1

    @command((Key(bytes), Int, bytes))
    def setrange(self, key, offset, value):
        if offset < 0:
            raise SimpleError(msgs.INVALID_OFFSET_MSG)
        elif not value:
            return len(key.get(b''))
        elif offset + len(value) > MAX_STRING_SIZE:
            raise SimpleError(msgs.STRING_OVERFLOW_MSG)
        else:
            out = key.get(b'')
            if len(out) < offset:
                out += b'\x00' * (offset - len(out))
            out = out[0:offset] + value + out[offset + len(value):]
            key.update(out)
            return len(out)

    @command((Key(bytes),))
    def strlen(self, key):
        return len(key.get(b''))

    # Hash commands

    @command((Key(Hash), bytes), (bytes,))
    def hdel(self, key, *fields):
        h = key.value
        rem = 0
        for field in fields:
            if field in h:
                del h[field]
                key.updated()
                rem += 1
        return rem

    @command((Key(Hash), bytes))
    def hexists(self, key, field):
        return int(field in key.value)

    @command((Key(Hash), bytes))
    def hget(self, key, field):
        return key.value.get(field)

    @command((Key(Hash),))
    def hgetall(self, key):
        return list(itertools.chain(*key.value.items()))

    @command((Key(Hash), bytes, Int))
    def hincrby(self, key, field, amount):
        c = Int.decode(key.value.get(field, b'0')) + amount
        key.value[field] = self._encodeint(c)
        key.updated()
        return c

    @command((Key(Hash), bytes, bytes))
    def hincrbyfloat(self, key, field, amount):
        c = Float.decode(key.value.get(field, b'0')) + Float.decode(amount)
        if not math.isfinite(c):
            raise SimpleError(msgs.NONFINITE_MSG)
        encoded = self._encodefloat(c, True)
        key.value[field] = encoded
        key.updated()
        return encoded

    @command((Key(Hash),))
    def hkeys(self, key):
        return list(key.value.keys())

    @command((Key(Hash),))
    def hlen(self, key):
        return len(key.value)

    @command((Key(Hash), bytes), (bytes,))
    def hmget(self, key, *fields):
        return [key.value.get(field) for field in fields]

    @command((Key(Hash), bytes, bytes), (bytes, bytes))
    def hmset(self, key, *args):
        self.hset(key, *args)
        return OK

    @command((Key(Hash), Int,), (bytes, bytes))
    def hscan(self, key, cursor, *args):
        cursor, keys = self._scan(key.value, cursor, *args)
        items = []
        for k in keys:
            items.append(k)
            items.append(key.value[k])
        return [cursor, items]

    @command((Key(Hash), bytes, bytes), (bytes, bytes))
    def hset(self, key, *args):
        h = key.value
        created = 0
        for i in range(0, len(args), 2):
            if args[i] not in h:
                created += 1
            h[args[i]] = args[i + 1]
        key.updated()
        return created

    @command((Key(Hash), bytes, bytes))
    def hsetnx(self, key, field, value):
        if field in key.value:
            return 0
        return self.hset(key, field, value)

    @command((Key(Hash), bytes))
    def hstrlen(self, key, field):
        return len(key.value.get(field, b''))

    @command((Key(Hash),))
    def hvals(self, key):
        return list(key.value.values())

    # List commands

    def _bpop_pass(self, keys, op, first_pass):
        for key in keys:
            item = CommandItem(key, self._db, item=self._db.get(key), default=[])
            if not isinstance(item.value, list):
                if first_pass:
                    raise SimpleError(msgs.WRONGTYPE_MSG)
                else:
                    continue
            if item.value:
                ret = op(item.value)
                item.updated()
                item.writeback()
                return [key, ret]
        return None

    def _bpop(self, args, op):
        keys = args[:-1]
        timeout = Timeout.decode(args[-1])
        return self._blocking(timeout, functools.partial(self._bpop_pass, keys, op))

    @command((bytes, bytes), (bytes,), flags='s')
    def blpop(self, *args):
        return self._bpop(args, lambda lst: lst.pop(0))

    @command((bytes, bytes), (bytes,), flags='s')
    def brpop(self, *args):
        return self._bpop(args, lambda lst: lst.pop())

    def _brpoplpush_pass(self, source, destination, first_pass):
        src = CommandItem(source, self._db, item=self._db.get(source), default=[])
        if not isinstance(src.value, list):
            if first_pass:
                raise SimpleError(msgs.WRONGTYPE_MSG)
            else:
                return None
        if not src.value:
            return None  # Empty list
        dst = CommandItem(destination, self._db, item=self._db.get(destination), default=[])
        if not isinstance(dst.value, list):
            raise SimpleError(msgs.WRONGTYPE_MSG)
        el = src.value.pop()
        dst.value.insert(0, el)
        src.updated()
        src.writeback()
        if destination != source:
            # Ensure writeback only happens once
            dst.updated()
            dst.writeback()
        return el

    @command((bytes, bytes, Timeout), flags='s')
    def brpoplpush(self, source, destination, timeout):
        return self._blocking(timeout,
                              functools.partial(self._brpoplpush_pass, source, destination))

    @command((Key(list, None), Int))
    def lindex(self, key, index):
        try:
            return key.value[index]
        except IndexError:
            return None

    @command((Key(list), bytes, bytes, bytes))
    def linsert(self, key, where, pivot, value):
        if not casematch(where, b'before') and not casematch(where, b'after'):
            raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        if not key:
            return 0
        else:
            try:
                index = key.value.index(pivot)
            except ValueError:
                return -1
            if casematch(where, b'after'):
                index += 1
            key.value.insert(index, value)
            key.updated()
            return len(key.value)

    @command((Key(list),))
    def llen(self, key):
        return len(key.value)

    @command((Key(list, None), Key(list), SimpleString, SimpleString))
    def lmove(self, first_list, second_list, src, dst):
        if src not in [b'LEFT', b'RIGHT']:
            raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        if dst not in [b'LEFT', b'RIGHT']:
            raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        el = self.rpop(first_list) if src == b'RIGHT' else self.lpop(first_list)
        self.lpush(second_list, el) if dst == b'LEFT' else self.rpush(second_list, el)
        return el

    def _list_pop(self, get_slice, key, *args):
        """Implements lpop and rpop.

        `get_slice` must take a count and return a slice expression for the
        range to pop.
        """
        # This implementation is somewhat contorted to match the odd
        # behaviours described in https://github.com/redis/redis/issues/9680.
        count = 1
        if len(args) > 1:
            raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        elif len(args) == 1:
            count = args[0]
            if count < 0:
                raise SimpleError(msgs.INDEX_ERROR_MSG)
            elif count == 0 and self.version == 6:
                return None
        if not key:
            return None
        elif type(key.value) != list:
            raise SimpleError(msgs.WRONGTYPE_MSG)
        slc = get_slice(count)
        ret = key.value[slc]
        del key.value[slc]
        key.updated()
        if not args:
            ret = ret[0]
        return ret

    @command((Key(),), (Int(),))
    def lpop(self, key, *args):
        return self._list_pop(lambda count: slice(None, count), key, *args)

    @command((Key(list), bytes), (bytes,))
    def lpush(self, key, *values):
        for value in values:
            key.value.insert(0, value)
        key.updated()
        return len(key.value)

    @command((Key(list), bytes), (bytes,))
    def lpushx(self, key, *values):
        if not key:
            return 0
        return self.lpush(key, *values)

    @command((Key(list), Int, Int))
    def lrange(self, key, start, stop):
        start, stop = self._fix_range(start, stop, len(key.value))
        return key.value[start:stop]

    @command((Key(list), Int, bytes))
    def lrem(self, key, count, value):
        a_list = key.value
        found = []
        for i, el in enumerate(a_list):
            if el == value:
                found.append(i)
        if count > 0:
            indices_to_remove = found[:count]
        elif count < 0:
            indices_to_remove = found[count:]
        else:
            indices_to_remove = found
        # Iterating in reverse order to ensure the indices
        # remain valid during deletion.
        for index in reversed(indices_to_remove):
            del a_list[index]
        if indices_to_remove:
            key.updated()
        return len(indices_to_remove)

    @command((Key(list), Int, bytes))
    def lset(self, key, index, value):
        if not key:
            raise SimpleError(msgs.NO_KEY_MSG)
        try:
            key.value[index] = value
            key.updated()
        except IndexError:
            raise SimpleError(msgs.INDEX_ERROR_MSG)
        return OK

    @command((Key(list), Int, Int))
    def ltrim(self, key, start, stop):
        if key:
            if stop == -1:
                stop = None
            else:
                stop += 1
            new_value = key.value[start:stop]
            # TODO: check if this should actually be conditional
            if len(new_value) != len(key.value):
                key.update(new_value)
        return OK

    @command((Key(),), (Int(),))
    def rpop(self, key, *args):
        return self._list_pop(lambda count: slice(None, -count - 1, -1), key, *args)

    @command((Key(list, None), Key(list)))
    def rpoplpush(self, src, dst):
        el = self.rpop(src)
        self.lpush(dst, el)
        return el

    @command((Key(list), bytes), (bytes,))
    def rpush(self, key, *values):
        for value in values:
            key.value.append(value)
        key.updated()
        return len(key.value)

    @command((Key(list), bytes), (bytes,))
    def rpushx(self, key, *values):
        if not key:
            return 0
        return self.rpush(key, *values)

    # Set commands

    @command((Key(set), bytes), (bytes,))
    def sadd(self, key, *members):
        old_size = len(key.value)
        key.value.update(members)
        key.updated()
        return len(key.value) - old_size

    @command((Key(set),))
    def scard(self, key):
        return len(key.value)

    @command((Key(set),), (Key(set),))
    def sdiff(self, *keys):
        return self._setop(lambda a, b: a - b, False, None, *keys)

    @command((Key(), Key(set)), (Key(set),))
    def sdiffstore(self, dst, *keys):
        return self._setop(lambda a, b: a - b, False, dst, *keys)

    @command((Key(set),), (Key(set),))
    def sinter(self, *keys):
        return self._setop(lambda a, b: a & b, True, None, *keys)

    @command((Key(), Key(set)), (Key(set),))
    def sinterstore(self, dst, *keys):
        return self._setop(lambda a, b: a & b, True, dst, *keys)

    @command((Key(set), bytes))
    def sismember(self, key, member):
        return int(member in key.value)

    @command((Key(set), bytes), (bytes,))
    def smismember(self, key, *members):
        return [self.sismember(key, member) for member in members]

    @command((Key(set),))
    def smembers(self, key):
        return list(key.value)

    @command((Key(set, 0), Key(set), bytes))
    def smove(self, src, dst, member):
        try:
            src.value.remove(member)
            src.updated()
        except KeyError:
            return 0
        else:
            dst.value.add(member)
            dst.updated()  # TODO: is it updated if member was already present?
            return 1

    @command((Key(set),), (Int,))
    def spop(self, key, count=None):
        if count is None:
            if not key.value:
                return None
            item = random.sample(list(key.value), 1)[0]
            key.value.remove(item)
            key.updated()
            return item
        else:
            if count < 0:
                raise SimpleError(msgs.INDEX_ERROR_MSG)
            items = self.srandmember(key, count)
            for item in items:
                key.value.remove(item)
                key.updated()  # Inside the loop because redis special-cases count=0
            return items

    @command((Key(set),), (Int,))
    def srandmember(self, key, count=None):
        if count is None:
            if not key.value:
                return None
            else:
                return random.sample(list(key.value), 1)[0]
        elif count >= 0:
            count = min(count, len(key.value))
            return random.sample(list(key.value), count)
        else:
            items = list(key.value)
            return [random.choice(items) for _ in range(-count)]

    @command((Key(set), bytes), (bytes,))
    def srem(self, key, *members):
        old_size = len(key.value)
        for member in members:
            key.value.discard(member)
        deleted = old_size - len(key.value)
        if deleted:
            key.updated()
        return deleted

    @command((Key(set), Int), (bytes, bytes))
    def sscan(self, key, cursor, *args):
        return self._scan(key.value, cursor, *args)

    @command((Key(set),), (Key(set),))
    def sunion(self, *keys):
        return self._setop(lambda a, b: a | b, False, None, *keys)

    @command((Key(), Key(set)), (Key(set),))
    def sunionstore(self, dst, *keys):
        return self._setop(lambda a, b: a | b, False, dst, *keys)

    # Hyperloglog commands
    # These are not quite the same as the real redis ones, which are
    # approximate and store the results in a string. Instead, it is implemented
    # on top of sets.

    @command((Key(set),), (bytes,))
    def pfadd(self, key, *elements):
        result = self.sadd(key, *elements)
        # Per the documentation:
        # - 1 if at least 1 HyperLogLog internal register was altered. 0 otherwise.
        return 1 if result > 0 else 0

    @command((Key(set),), (Key(set),))
    def pfcount(self, *keys):
        """
        Return the approximated cardinality of
        the set observed by the HyperLogLog at key(s).
        """
        return len(self.sunion(*keys))

    @command((Key(set), Key(set)), (Key(set),))
    def pfmerge(self, dest, *sources):
        "Merge N different HyperLogLogs into a single one."
        self.sunionstore(dest, *sources)
        return OK

    # Sorted set commands
    # TODO: [b]zpopmin/zpopmax,

    @staticmethod
    def _limit_items(items, offset, count):
        out = []
        for item in items:
            if offset:  # Note: not offset > 0, in order to match redis
                offset -= 1
                continue
            if count == 0:
                break
            count -= 1
            out.append(item)
        return out

    def _apply_withscores(self, items, withscores):
        if withscores:
            out = []
            for item in items:
                out.append(item[1])
                out.append(self._encodefloat(item[0], False))
        else:
            out = [item[1] for item in items]
        return out

    @command((Key(ZSet), bytes, bytes), (bytes,))
    def zadd(self, key, *args):
        zset = key.value
        ZADD_PARAMS = ['nx', 'xx', 'ch', 'incr', 'gt', 'lt', ]
        param_val = {k: False for k in ZADD_PARAMS}
        i = 0

        while i < len(args):
            found = False
            for param in ZADD_PARAMS:
                if casematch(args[i], bytes(param, encoding='utf8')):
                    param_val[param] = True
                    found = True
                    break
            if found:
                i += 1
                continue
            # First argument not matching flags indicates the start of
            # score pairs.
            break

        if param_val['nx'] and param_val['xx']:
            raise SimpleError(msgs.ZADD_NX_XX_ERROR_MSG)
        if [param_val['nx'], param_val['gt'], param_val['lt']].count(True) > 1:
            raise SimpleError(msgs.ZADD_NX_GT_LT_ERROR_MSG)
        elements = args[i:]
        if not elements or len(elements) % 2 != 0:
            raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        if param_val['incr'] and len(elements) != 2:
            raise SimpleError(msgs.ZADD_INCR_LEN_ERROR_MSG)
        # Parse all scores first, before updating
        items = [
            (0.0 + Float.decode(elements[j]) if self.version >= 7 else Float.decode(elements[j]), elements[j + 1])
            for j in range(0, len(elements), 2)
        ]
        old_len = len(zset)
        changed_items = 0

        if param_val['incr']:
            item_score, item_name = items[0]
            if (param_val['nx'] and item_name in zset) or (param_val['xx'] and item_name not in zset):
                return None
            return self.zincrby(key, item_score, item_name)

        for item_score, item_name in items:
            if (
                    (param_val['nx'] and item_name not in zset)
                    or (param_val['xx'] and item_name in zset)
                    or (param_val['gt'] and ((item_name in zset and zset.get(item_name) < item_score)
                                             or (not param_val['xx'] and item_name not in zset)))
                    or (param_val['lt'] and ((item_name in zset and zset.get(item_name) > item_score)
                                             or (not param_val['xx'] and item_name not in zset)))
                    or ([param_val['nx'], param_val['gt'], param_val['lt'], param_val['xx']].count(True) == 0)
            ):
                if zset.add(item_name, item_score):
                    changed_items += 1

        if changed_items:
            key.updated()

        if param_val['ch']:
            return changed_items
        return len(zset) - old_len

    @command((Key(ZSet),))
    def zcard(self, key):
        return len(key.value)

    @command((Key(ZSet), ScoreTest, ScoreTest))
    def zcount(self, key, min, max):
        return key.value.zcount(min.lower_bound, max.upper_bound)

    @command((Key(ZSet), Float, bytes))
    def zincrby(self, key, increment, member):
        # Can't just default the old score to 0.0, because in IEEE754, adding
        # 0.0 to something isn't a nop (e.g. 0.0 + -0.0 == 0.0).
        try:
            score = key.value.get(member, None) + increment
        except TypeError:
            score = increment
        if math.isnan(score):
            raise SimpleError(msgs.SCORE_NAN_MSG)
        key.value[member] = score
        key.updated()
        return self._encodefloat(score, False)

    @command((Key(ZSet), StringTest, StringTest))
    def zlexcount(self, key, min, max):
        return key.value.zlexcount(min.value, min.exclusive, max.value, max.exclusive)

    def _zrange(self, key, start, stop, reverse, *args):
        zset = key.value
        withscores = False
        byscore = False
        for arg in args:
            if casematch(arg, b'withscores'):
                withscores = True
            elif casematch(arg, b'byscore'):
                byscore = True
            else:
                raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        if byscore:
            items = zset.irange_score(start.lower_bound, stop.upper_bound, reverse=reverse)
        else:
            start, stop = Int.decode(start.bytes_val), Int.decode(stop.bytes_val)
            start, stop = self._fix_range(start, stop, len(zset))
            if reverse:
                start, stop = len(zset) - stop, len(zset) - start
            items = zset.islice_score(start, stop, reverse)
        items = self._apply_withscores(items, withscores)
        return items

    @command((Key(ZSet), ScoreTest, ScoreTest), (bytes,))
    def zrange(self, key, start, stop, *args):
        return self._zrange(key, start, stop, False, *args)

    @command((Key(ZSet), ScoreTest, ScoreTest), (bytes,))
    def zrevrange(self, key, start, stop, *args):
        return self._zrange(key, start, stop, True, *args)

    def _zrangebylex(self, key, _min, _max, reverse, *args):
        if args:
            if len(args) != 3 or not casematch(args[0], b'limit'):
                raise SimpleError(msgs.SYNTAX_ERROR_MSG)
            offset = Int.decode(args[1])
            count = Int.decode(args[2])
        else:
            offset = 0
            count = -1
        zset = key.value
        items = zset.irange_lex(_min.value, _max.value,
                                inclusive=(not _min.exclusive, not _max.exclusive),
                                reverse=reverse)
        items = self._limit_items(items, offset, count)
        return items

    @command((Key(ZSet), StringTest, StringTest), (bytes,))
    def zrangebylex(self, key, _min, _max, *args):
        return self._zrangebylex(key, _min, _max, False, *args)

    @command((Key(ZSet), StringTest, StringTest), (bytes,))
    def zrevrangebylex(self, key, _max, _min, *args):
        return self._zrangebylex(key, _min, _max, True, *args)

    def _zrangebyscore(self, key, _min, _max, reverse, *args):
        withscores = False
        offset = 0
        count = -1
        i = 0
        while i < len(args):
            if casematch(args[i], b'withscores'):
                withscores = True
                i += 1
            elif casematch(args[i], b'limit') and i + 2 < len(args):
                offset = Int.decode(args[i + 1])
                count = Int.decode(args[i + 2])
                i += 3
            else:
                raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        zset = key.value
        items = list(zset.irange_score(_min.lower_bound, _max.upper_bound, reverse=reverse))
        items = self._limit_items(items, offset, count)
        items = self._apply_withscores(items, withscores)
        return items

    @command((Key(ZSet), ScoreTest, ScoreTest), (bytes,))
    def zrangebyscore(self, key, _min, _max, *args):
        return self._zrangebyscore(key, _min, _max, False, *args)

    @command((Key(ZSet), ScoreTest, ScoreTest), (bytes,))
    def zrevrangebyscore(self, key, _max, _min, *args):
        return self._zrangebyscore(key, _min, _max, True, *args)

    @command((Key(ZSet), bytes))
    def zrank(self, key, member):
        try:
            return key.value.rank(member)
        except KeyError:
            return None

    @command((Key(ZSet), bytes))
    def zrevrank(self, key, member):
        try:
            return len(key.value) - 1 - key.value.rank(member)
        except KeyError:
            return None

    @command((Key(ZSet), bytes), (bytes,))
    def zrem(self, key, *members):
        old_size = len(key.value)
        for member in members:
            key.value.discard(member)
        deleted = old_size - len(key.value)
        if deleted:
            key.updated()
        return deleted

    @command((Key(ZSet), StringTest, StringTest))
    def zremrangebylex(self, key, min, max):
        items = key.value.irange_lex(min.value, max.value,
                                     inclusive=(not min.exclusive, not max.exclusive))
        return self.zrem(key, *items)

    @command((Key(ZSet), ScoreTest, ScoreTest))
    def zremrangebyscore(self, key, min, max):
        items = key.value.irange_score(min.lower_bound, max.upper_bound)
        return self.zrem(key, *[item[1] for item in items])

    @command((Key(ZSet), Int, Int))
    def zremrangebyrank(self, key, start, stop):
        zset = key.value
        start, stop = self._fix_range(start, stop, len(zset))
        items = zset.islice_score(start, stop)
        return self.zrem(key, *[item[1] for item in items])

    @command((Key(ZSet), Int), (bytes, bytes))
    def zscan(self, key, cursor, *args):
        new_cursor, ans = self._scan(key.value.items(), cursor, *args)
        flat = []
        for (key, score) in ans:
            flat.append(key)
            flat.append(self._encodefloat(score, False))
        return [new_cursor, flat]

    @command((Key(ZSet), bytes))
    def zscore(self, key, member):
        try:
            return self._encodefloat(key.value[member], False)
        except KeyError:
            return None

    @staticmethod
    def _get_zset(value):
        if isinstance(value, set):
            zset = ZSet()
            for item in value:
                zset[item] = 1.0
            return zset
        elif isinstance(value, ZSet):
            return value
        else:
            raise SimpleError(msgs.WRONGTYPE_MSG)

    def _zunioninter(self, func, dest, numkeys, *args):
        if numkeys < 1:
            raise SimpleError(msgs.ZUNIONSTORE_KEYS_MSG)
        if numkeys > len(args):
            raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        aggregate = b'sum'
        sets = []
        for i in range(numkeys):
            item = CommandItem(args[i], self._db, item=self._db.get(args[i]), default=ZSet())
            sets.append(self._get_zset(item.value))
        weights = [1.0] * numkeys

        i = numkeys
        while i < len(args):
            arg = args[i]
            if casematch(arg, b'weights') and i + numkeys < len(args):
                weights = [Float.decode(x) for x in args[i + 1:i + numkeys + 1]]
                i += numkeys + 1
            elif casematch(arg, b'aggregate') and i + 1 < len(args):
                aggregate = casenorm(args[i + 1])
                if aggregate not in (b'sum', b'min', b'max'):
                    raise SimpleError(msgs.SYNTAX_ERROR_MSG)
                i += 2
            else:
                raise SimpleError(msgs.SYNTAX_ERROR_MSG)

        out_members = set(sets[0])
        for s in sets[1:]:
            if func == 'ZUNIONSTORE':
                out_members |= set(s)
            else:
                out_members.intersection_update(s)

        # We first build a regular dict and turn it into a ZSet. The
        # reason is subtle: a ZSet won't update a score from -0 to +0
        # (or vice versa) through assignment, but a regular dict will.
        out = {}
        # The sort affects the order of floating-point operations.
        # Note that redis uses qsort(1), which has no stability guarantees,
        # so we can't be sure to match it in all cases.
        for s, w in sorted(zip(sets, weights), key=lambda x: len(x[0])):
            for member, score in s.items():
                score *= w
                # Redis only does this step for ZUNIONSTORE. See
                # https://github.com/antirez/redis/issues/3954.
                if func == 'ZUNIONSTORE' and math.isnan(score):
                    score = 0.0
                if member not in out_members:
                    continue
                if member in out:
                    old = out[member]
                    if aggregate == b'sum':
                        score += old
                        if math.isnan(score):
                            score = 0.0
                    elif aggregate == b'max':
                        score = max(old, score)
                    elif aggregate == b'min':
                        score = min(old, score)
                    else:
                        assert False  # pragma: nocover
                if math.isnan(score):
                    score = 0.0
                out[member] = score

        out_zset = ZSet()
        for member, score in out.items():
            out_zset[member] = score

        dest.value = out_zset
        return len(out_zset)

    @command((Key(), Int, bytes), (bytes,))
    def zunionstore(self, dest, numkeys, *args):
        return self._zunioninter('ZUNIONSTORE', dest, numkeys, *args)

    @command((Key(), Int, bytes), (bytes,))
    def zinterstore(self, dest, numkeys, *args):
        return self._zunioninter('ZINTERSTORE', dest, numkeys, *args)

    # Server commands
    # TODO: lots

    @command((), (bytes,), flags='s')
    def bgsave(self, *args):
        if len(args) > 1 or (len(args) == 1 and not casematch(args[0], b'schedule')):
            raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        self._server.lastsave = int(time.time())
        return BGSAVE_STARTED

    @command(())
    def dbsize(self):
        return len(self._db)

    @command((), (bytes,))
    def flushdb(self, *args):
        if args:
            if len(args) != 1 or not casematch(args[0], b'async'):
                raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        self._db.clear()
        return OK

    @command((), (bytes,))
    def flushall(self, *args):
        if args:
            if len(args) != 1 or not casematch(args[0], b'async'):
                raise SimpleError(msgs.SYNTAX_ERROR_MSG)
        for db in self._server.dbs.values():
            db.clear()
        # TODO: clear watches and/or pubsub as well?
        return OK

    @command(())
    def lastsave(self):
        return self._server.lastsave

    @command((), flags='s')
    def save(self):
        self._server.lastsave = int(time.time())
        return OK

    @command(())
    def time(self):
        now_us = round(time.time() * 1000000)
        now_s = now_us // 1000000
        now_us %= 1000000
        return [str(now_s).encode(), str(now_us).encode()]

    # Script commands
    # script debug and script kill will probably not be supported

    def _convert_redis_arg(self, lua_runtime, value):
        # Type checks are exact to avoid issues like bool being a subclass of int.
        if type(value) is bytes:
            return value
        elif type(value) in {int, float}:
            return '{:.17g}'.format(value).encode()
        else:
            # TODO: add the context
            msg = LUA_COMMAND_ARG_MSG6 if self.version < 7 else LUA_COMMAND_ARG_MSG
            raise SimpleError(msg)

    def _convert_redis_result(self, lua_runtime, result):
        if isinstance(result, (bytes, int)):
            return result
        elif isinstance(result, SimpleString):
            return lua_runtime.table_from({b"ok": result.value})
        elif result is None:
            return False
        elif isinstance(result, list):
            converted = [
                self._convert_redis_result(lua_runtime, item)
                for item in result
            ]
            return lua_runtime.table_from(converted)
        elif isinstance(result, SimpleError):
            raise result
        else:
            raise RuntimeError("Unexpected return type from redis: {}".format(type(result)))

    def _convert_lua_result(self, result, nested=True):
        from lupa import lua_type
        if lua_type(result) == 'table':
            for key in (b'ok', b'err'):
                if key in result:
                    msg = self._convert_lua_result(result[key])
                    if not isinstance(msg, bytes):
                        raise SimpleError(msgs.LUA_WRONG_NUMBER_ARGS_MSG)
                    if key == b'ok':
                        return SimpleString(msg)
                    elif nested:
                        return SimpleError(msg.decode('utf-8', 'replace'))
                    else:
                        raise SimpleError(msg.decode('utf-8', 'replace'))
            # Convert Lua tables into lists, starting from index 1, mimicking the behavior of StrictRedis.
            result_list = []
            for index in itertools.count(1):
                if index not in result:
                    break
                item = result[index]
                result_list.append(self._convert_lua_result(item))
            return result_list
        elif isinstance(result, str):
            return result.encode()
        elif isinstance(result, float):
            return int(result)
        elif isinstance(result, bool):
            return 1 if result else None
        return result

    def ensure_str(self, s):
        return (s.decode(encoding='utf-8', errors='replace')
                if isinstance(s, bytes)
                else str(s).encode(encoding='utf-8', errors='replace'))

    def _check_for_lua_globals(self, lua_runtime, expected_globals):
        actual_globals = set(lua_runtime.globals().keys())
        if actual_globals != expected_globals:
            unexpected = [self.ensure_str(var, 'utf-8', 'replace')
                          for var in actual_globals - expected_globals]
            raise SimpleError(msgs.GLOBAL_VARIABLE_MSG.format(", ".join(unexpected)))

    def _lua_redis_call(self, lua_runtime, expected_globals, op, *args):
        # Check if we've set any global variables before making any change.
        self._check_for_lua_globals(lua_runtime, expected_globals)
        func, func_name = self._name_to_func(op)
        args = [self._convert_redis_arg(lua_runtime, arg) for arg in args]
        result = self._run_command(func, func._fakeredis_sig, args, True)
        return self._convert_redis_result(lua_runtime, result)

    def _lua_redis_pcall(self, lua_runtime, expected_globals, op, *args):
        try:
            return self._lua_redis_call(lua_runtime, expected_globals, op, *args)
        except Exception as ex:
            return lua_runtime.table_from({b"err": str(ex)})

    def _lua_redis_log(self, lua_runtime, expected_globals, lvl, *args):
        self._check_for_lua_globals(lua_runtime, expected_globals)
        if len(args) < 1:
            raise SimpleError(msgs.REQUIRES_MORE_ARGS_MSG.format("redis.log()", "two"))
        if lvl not in REDIS_LOG_LEVELS.values():
            raise SimpleError(msgs.LOG_INVALID_DEBUG_LEVEL_MSG)
        msg = ' '.join([x.decode('utf-8')
                        if isinstance(x, bytes) else str(x)
                        for x in args if not isinstance(x, bool)])
        LOGGER.log(REDIS_LOG_LEVELS_TO_LOGGING[lvl], msg)

    @command((bytes, Int), (bytes,), flags='s')
    def eval(self, script, numkeys, *keys_and_args):
        from lupa import LuaError, LuaRuntime, as_attrgetter

        if numkeys > len(keys_and_args):
            raise SimpleError(msgs.TOO_MANY_KEYS_MSG)
        if numkeys < 0:
            raise SimpleError(msgs.NEGATIVE_KEYS_MSG)
        sha1 = hashlib.sha1(script).hexdigest().encode()
        self._server.script_cache[sha1] = script
        lua_runtime = LuaRuntime(encoding=None, unpack_returned_tuples=True)

        set_globals = lua_runtime.eval(
            """
            function(keys, argv, redis_call, redis_pcall, redis_log, redis_log_levels)
                redis = {}
                redis.call = redis_call
                redis.pcall = redis_pcall
                redis.log = redis_log
                for level, pylevel in python.iterex(redis_log_levels.items()) do
                    redis[level] = pylevel
                end
                redis.error_reply = function(msg) return {err=msg} end
                redis.status_reply = function(msg) return {ok=msg} end
                KEYS = keys
                ARGV = argv
            end
            """
        )
        expected_globals = set()
        set_globals(
            lua_runtime.table_from(keys_and_args[:numkeys]),
            lua_runtime.table_from(keys_and_args[numkeys:]),
            functools.partial(self._lua_redis_call, lua_runtime, expected_globals),
            functools.partial(self._lua_redis_pcall, lua_runtime, expected_globals),
            functools.partial(self._lua_redis_log, lua_runtime, expected_globals),
            as_attrgetter(REDIS_LOG_LEVELS)
        )
        expected_globals.update(lua_runtime.globals().keys())

        try:
            result = lua_runtime.execute(script)
        except SimpleError as ex:
            if self.version == 6:
                raise SimpleError(msgs.SCRIPT_ERROR_MSG.format(sha1.decode(), ex))
            raise SimpleError(ex.value)
        except LuaError as ex:
            raise SimpleError(msgs.SCRIPT_ERROR_MSG.format(sha1.decode(), ex))

        self._check_for_lua_globals(lua_runtime, expected_globals)

        return self._convert_lua_result(result, nested=False)

    @command((bytes, Int), (bytes,), flags='s')
    def evalsha(self, sha1, numkeys, *keys_and_args):
        try:
            script = self._server.script_cache[sha1]
        except KeyError:
            raise SimpleError(msgs.NO_MATCHING_SCRIPT_MSG)
        return self.eval(script, numkeys, *keys_and_args)

    @command((bytes,), (bytes,), flags='s')
    def script(self, subcmd, *args):
        if casematch(subcmd, b'load'):
            if len(args) != 1:
                raise SimpleError(msgs.BAD_SUBCOMMAND_MSG.format('SCRIPT'))
            script = args[0]
            sha1 = hashlib.sha1(script).hexdigest().encode()
            self._server.script_cache[sha1] = script
            return sha1
        elif casematch(subcmd, b'exists'):
            if self.version >= 7 and len(args) == 0:
                raise SimpleError(msgs.WRONG_ARGS_MSG.format('script|exists'))
            return [int(sha1 in self._server.script_cache) for sha1 in args]
        elif casematch(subcmd, b'flush'):
            if len(args) > 1 or (len(args) == 1 and casenorm(args[0]) not in {b'sync', b'async'}):
                raise SimpleError(msgs.BAD_SUBCOMMAND_MSG.format('SCRIPT'))
            self._server.script_cache = {}
            return OK
        else:
            raise SimpleError(msgs.BAD_SUBCOMMAND_MSG.format('SCRIPT'))

    @command((bytes,), (bytes,), flags='s')
    def psubscribe(self, *patterns):
        return self._subscribe(patterns, self._server.psubscribers, b'psubscribe')

    @command((bytes,), (bytes,), flags='s')
    def subscribe(self, *channels):
        return self._subscribe(channels, self._server.subscribers, b'subscribe')

    @command((), (bytes,), flags='s')
    def punsubscribe(self, *patterns):
        return self._unsubscribe(patterns, self._server.psubscribers, b'punsubscribe')

    @command((), (bytes,), flags='s')
    def unsubscribe(self, *channels):
        return self._unsubscribe(channels, self._server.subscribers, b'unsubscribe')

    @command((bytes, bytes))
    def publish(self, channel, message):
        receivers = 0
        msg = [b'message', channel, message]
        subs = self._server.subscribers.get(channel, set())
        for sock in subs:
            sock.put_response(msg)
            receivers += 1
        for (pattern, socks) in self._server.psubscribers.items():
            regex = compile_pattern(pattern)
            if regex.match(channel):
                msg = [b'pmessage', pattern, channel, message]
                for sock in socks:
                    sock.put_response(msg)
                    receivers += 1
        return receivers

    def _encodefloat(self, value, humanfriendly):
        if self.version >= 7:
            value = 0 + value
        return Float.encode(value, humanfriendly)

    def _encodeint(self, value):
        if self.version >= 7:
            value = 0 + value
        return Int.encode(value)


setattr(FakeSocket, 'del', FakeSocket.del_)
delattr(FakeSocket, 'del_')
setattr(FakeSocket, 'set', FakeSocket.set_)
delattr(FakeSocket, 'set_')
setattr(FakeSocket, 'exec', FakeSocket.exec_)
delattr(FakeSocket, 'exec_')
