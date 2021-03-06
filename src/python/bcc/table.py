# Copyright 2015 PLUMgrid
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import MutableMapping
import ctypes as ct
import multiprocessing

from .libbcc import lib, _RAW_CB_TYPE

BPF_MAP_TYPE_HASH = 1
BPF_MAP_TYPE_ARRAY = 2
BPF_MAP_TYPE_PROG_ARRAY = 3
BPF_MAP_TYPE_PERF_EVENT_ARRAY = 4

stars_max = 40

# helper functions, consider moving these to a utils module
def _stars(val, val_max, width):
    i = 0
    text = ""
    while (1):
        if (i > (width * val / val_max) - 1) or (i > width - 1):
            break
        text += "*"
        i += 1
    if val > val_max:
        text = text[:-1] + "+"
    return text


def _print_log2_hist(vals, val_type):
    global stars_max
    log2_dist_max = 64
    idx_max = -1
    val_max = 0

    for i, v in enumerate(vals):
        if v > 0: idx_max = i
        if v > val_max: val_max = v

    if idx_max <= 32:
        header = "     %-19s : count     distribution"
        body = "%10d -> %-10d : %-8d |%-*s|"
        stars = stars_max
    else:
        header = "               %-29s : count     distribution"
        body = "%20d -> %-20d : %-8d |%-*s|"
        stars = int(stars_max / 2)

    if idx_max > 0:
        print(header % val_type);
    for i in range(1, idx_max + 1):
        low = (1 << i) >> 1
        high = (1 << i) - 1
        if (low == high):
            low -= 1
        val = vals[i]
        print(body % (low, high, val, stars,
                      _stars(val, val_max, stars)))


def Table(bpf, map_id, map_fd, keytype, leaftype):
    """Table(bpf, map_id, map_fd, keytype, leaftype)

    Create a python object out of a reference to a bpf table handle"""

    ttype = lib.bpf_table_type_id(bpf.module, map_id)
    t = None
    if ttype == BPF_MAP_TYPE_HASH:
        t = HashTable(bpf, map_id, map_fd, keytype, leaftype)
    elif ttype == BPF_MAP_TYPE_ARRAY:
        t = Array(bpf, map_id, map_fd, keytype, leaftype)
    elif ttype == BPF_MAP_TYPE_PROG_ARRAY:
        t = ProgArray(bpf, map_id, map_fd, keytype, leaftype)
    elif ttype == BPF_MAP_TYPE_PERF_EVENT_ARRAY:
        t = PerfEventArray(bpf, map_id, map_fd, keytype, leaftype)
    if t == None:
        raise Exception("Unknown table type %d" % ttype)
    return t


class TableBase(MutableMapping):

    def __init__(self, bpf, map_id, map_fd, keytype, leaftype):
        self.bpf = bpf
        self.map_id = map_id
        self.map_fd = map_fd
        self.Key = keytype
        self.Leaf = leaftype
        self.ttype = lib.bpf_table_type_id(self.bpf.module, self.map_id)
        self._cbs = {}

    def key_sprintf(self, key):
        key_p = ct.pointer(key)
        buf = ct.create_string_buffer(ct.sizeof(self.Key) * 8)
        res = lib.bpf_table_key_snprintf(self.bpf.module, self.map_id,
                buf, len(buf), key_p)
        if res < 0:
            raise Exception("Could not printf key")
        return buf.value

    def leaf_sprintf(self, leaf):
        leaf_p = ct.pointer(leaf)
        buf = ct.create_string_buffer(ct.sizeof(self.Leaf) * 8)
        res = lib.bpf_table_leaf_snprintf(self.bpf.module, self.map_id,
                buf, len(buf), leaf_p)
        if res < 0:
            raise Exception("Could not printf leaf")
        return buf.value

    def key_scanf(self, key_str):
        key = self.Key()
        key_p = ct.pointer(key)
        res = lib.bpf_table_key_sscanf(self.bpf.module, self.map_id,
                key_str, key_p)
        if res < 0:
            raise Exception("Could not scanf key")
        return key

    def leaf_scanf(self, leaf_str):
        leaf = self.Leaf()
        leaf_p = ct.pointer(leaf)
        res = lib.bpf_table_leaf_sscanf(self.bpf.module, self.map_id,
                leaf_str, leaf_p)
        if res < 0:
            raise Exception("Could not scanf leaf")
        return leaf

    def __getitem__(self, key):
        key_p = ct.pointer(key)
        leaf = self.Leaf()
        leaf_p = ct.pointer(leaf)
        res = lib.bpf_lookup_elem(self.map_fd,
                ct.cast(key_p, ct.c_void_p),
                ct.cast(leaf_p, ct.c_void_p))
        if res < 0:
            raise KeyError
        return leaf

    def __setitem__(self, key, leaf):
        key_p = ct.pointer(key)
        leaf_p = ct.pointer(leaf)
        res = lib.bpf_update_elem(self.map_fd,
                ct.cast(key_p, ct.c_void_p),
                ct.cast(leaf_p, ct.c_void_p), 0)
        if res < 0:
            raise Exception("Could not update table")

    # override the MutableMapping's implementation of these since they
    # don't handle KeyError nicely
    def itervalues(self):
        for key in self:
            # a map entry may be deleted in between discovering the key and
            # fetching the value, suppress such errors
            try:
                yield self[key]
            except KeyError:
                pass

    def iteritems(self):
        for key in self:
            try:
                yield (key, self[key])
            except KeyError:
                pass

    def items(self):
        return [item for item in self.iteritems()]

    def values(self):
        return [value for value in self.itervalues()]

    def clear(self):
        # default clear uses popitem, which can race with the bpf prog
        for k in self.keys():
            self.__delitem__(k)

    def zero(self):
        for k in self.keys():
            self[k] = self.Leaf()

    def __iter__(self):
        return TableBase.Iter(self, self.Key)

    def iter(self): return self.__iter__()
    def keys(self): return self.__iter__()

    class Iter(object):
        def __init__(self, table, keytype):
            self.Key = keytype
            self.table = table
            k = self.Key()
            kp = ct.pointer(k)
            # if 0 is a valid key, try a few alternatives
            if k in table:
                ct.memset(kp, 0xff, ct.sizeof(k))
                if k in table:
                    ct.memset(kp, 0x55, ct.sizeof(k))
                    if k in table:
                        raise Exception("Unable to allocate iterator")
            self.key = k
        def __iter__(self):
            return self
        def __next__(self):
            return self.next()
        def next(self):
            self.key = self.table.next(self.key)
            return self.key

    def next(self, key):
        next_key = self.Key()
        next_key_p = ct.pointer(next_key)
        key_p = ct.pointer(key)
        res = lib.bpf_get_next_key(self.map_fd,
                ct.cast(key_p, ct.c_void_p),
                ct.cast(next_key_p, ct.c_void_p))
        if res < 0:
            raise StopIteration()
        return next_key

    def print_log2_hist(self, val_type="value", section_header="Bucket ptr",
            section_print_fn=None):
        """print_log2_hist(val_type="value", section_header="Bucket ptr",
                           section_print_fn=None)

        Prints a table as a log2 histogram. The table must be stored as
        log2. The val_type argument is optional, and is a column header.
        If the histogram has a secondary key, multiple tables will print
        and section_header can be used as a header description for each.
        If section_print_fn is not None, it will be passed the bucket value
        to format into a string as it sees fit.
        """
        if isinstance(self.Key(), ct.Structure):
            tmp = {}
            f1 = self.Key._fields_[0][0]
            f2 = self.Key._fields_[1][0]
            for k, v in self.items():
                bucket = getattr(k, f1)
                vals = tmp[bucket] = tmp.get(bucket, [0] * 65)
                slot = getattr(k, f2)
                vals[slot] = v.value
            for bucket, vals in tmp.items():
                if section_print_fn:
                    print("\n%s = %s" % (section_header,
                        section_print_fn(bucket)))
                else:
                    print("\n%s = %r" % (section_header, bucket))
                _print_log2_hist(vals, val_type)
        else:
            vals = [0] * 65
            for k, v in self.items():
                vals[k.value] = v.value
            _print_log2_hist(vals, val_type)


class HashTable(TableBase):
    def __init__(self, *args, **kwargs):
        super(HashTable, self).__init__(*args, **kwargs)

    def __len__(self):
        i = 0
        for k in self: i += 1
        return i

    def __delitem__(self, key):
        key_p = ct.pointer(key)
        res = lib.bpf_delete_elem(self.map_fd, ct.cast(key_p, ct.c_void_p))
        if res < 0:
            raise KeyError


class ArrayBase(TableBase):
    def __init__(self, *args, **kwargs):
        super(ArrayBase, self).__init__(*args, **kwargs)
        self.max_entries = int(lib.bpf_table_max_entries_id(self.bpf.module,
                self.map_id))

    def _normalize_key(self, key):
        if isinstance(key, int):
            if key < 0:
                key = len(self) + key
            key = self.Key(key)
        if not isinstance(key, ct._SimpleCData):
            raise IndexError("Array index must be an integer type")
        if key.value >= len(self):
            raise IndexError("Array index out of range")
        return key

    def __len__(self):
        return self.max_entries

    def __getitem__(self, key):
        key = self._normalize_key(key)
        return super(ArrayBase, self).__getitem__(key)

    def __setitem__(self, key, leaf):
        key = self._normalize_key(key)
        super(ArrayBase, self).__setitem__(key, leaf)

    def __delitem__(self, key):
        key = self._normalize_key(key)
        key_p = ct.pointer(key)

        # Deleting from array type maps does not have an effect, so
        # zero out the entry instead.
        leaf = self.Leaf()
        leaf_p = ct.pointer(leaf)
        res = lib.bpf_update_elem(self.map_fd, ct.cast(key_p, ct.c_void_p),
                ct.cast(leaf_p, ct.c_void_p), 0)
        if res < 0:
            raise Exception("Could not clear item")

    def __iter__(self):
        return ArrayBase.Iter(self, self.Key)

    class Iter(object):
        def __init__(self, table, keytype):
            self.Key = keytype
            self.table = table
            self.i = -1

        def __iter__(self):
            return self
        def __next__(self):
            return self.next()
        def next(self):
            self.i += 1
            if self.i == len(self.table):
                raise StopIteration()
            return self.Key(self.i)

class Array(ArrayBase):
    def __init__(self, *args, **kwargs):
        super(Array, self).__init__(*args, **kwargs)


class ProgArray(ArrayBase):
    def __init__(self, *args, **kwargs):
        super(ProgArray, self).__init__(*args, **kwargs)

    def __setitem__(self, key, leaf):
        if isinstance(leaf, int):
            leaf = self.Leaf(leaf)
        if isinstance(leaf, self.bpf.Function):
            leaf = self.Leaf(leaf.fd)
        super(ProgArray, self).__setitem__(key, leaf)


class PerfEventArray(ArrayBase):
    def __init__(self, *args, **kwargs):
        super(PerfEventArray, self).__init__(*args, **kwargs)

    def __delitem__(self, key):
        super(PerfEventArray, self).__init__(key)
        self.close_perf_buffer(key)

    def open_perf_buffer(self, callback):
        """open_perf_buffers(callback)

        Opens a set of per-cpu ring buffer to receive custom perf event
        data from the bpf program. The callback will be invoked for each
        event submitted from the kernel, up to millions per second.
        """

        for i in range(0, multiprocessing.cpu_count()):
            self._open_perf_buffer(i, callback)

    def _open_perf_buffer(self, cpu, callback):
        fn = _RAW_CB_TYPE(lambda _, data, size: callback(cpu, data, size))
        reader = lib.bpf_open_perf_buffer(fn, None, -1, cpu)
        if not reader:
            raise Exception("Could not open perf buffer")
        fd = lib.perf_reader_fd(reader)
        self[self.Key(cpu)] = self.Leaf(fd)
        self.bpf.open_kprobes()[(id(self), cpu)] = reader
        # keep a refcnt
        self._cbs[cpu] = fn

    def close_perf_buffer(self, key):
        reader = self.bpf.open_kprobes().get((id(self), key))
        if reader:
            lib.perf_reader_free(reader)
            del(self.bpf.open_kprobes()[(id(self), key)])
        del self._cbs[key]
