# -*- coding: utf-8 -*-

# Copyright (c) 2013-2018 NASK. All rights reserved.
#
# For some code in this module:
# Copyright (c) 2001-2013 Python Software Foundation. All rights reserved.
# (For more information -- see the docstrings below...)

import abc
import ast
import collections
import copy
import cPickle
import functools
import hashlib
import itertools
import operator
import os
import os.path as osp
import random
import re
import shutil
import subprocess
import sys
import tempfile
import thread
import threading
import time
import traceback
import weakref

from pkg_resources import cleanup_resources
from pyramid.decorator import reify

# for backward-compatibility and/or for convenience, the following
# constants and functions importable from some of the n6sdk.* modules
# are also accessible via this module:
from n6sdk.addr_helpers import (
    ip_network_as_tuple,
    ip_network_tuple_to_min_max_ip,
    ip_str_to_int,
)
from n6sdk.encoding_helpers import (
    ascii_str,
    py_identifier_str,
    as_unicode,
    provide_surrogateescape,
    string_to_bool,
)
from n6sdk.regexes import (
    CC_SIMPLE_REGEX,
    DOMAIN_ASCII_LOWERCASE_REGEX,
    DOMAIN_ASCII_LOWERCASE_STRICT_REGEX,
    IPv4_STRICT_DECIMAL_REGEX,  # <- NOTE: not (yet?) used by this module's IP-related functions
    IPv4_ANONYMIZED_REGEX,
    IPv4_CIDR_NETWORK_REGEX,
    PY_IDENTIFIER_REGEX,
)
from n6lib.const import (
    HOSTNAME,
    SCRIPT_BASENAME,
)


# extremely simplified URL match()-only regex
# (among others, n6lib.record_dict.url_preadjuster() makes use of it)
URL_SIMPLE_REGEX = re.compile(r'(?P<scheme>[\-+.0-9a-zA-Z]+)'
                              r'(?P<rest>:.*)')

# more restrictive than actual e-mail address syntax but sensible in most cases
EMAIL_OVERRESTRICTED_SIMPLE_REGEX = re.compile(r'''
        \A
        (?P<local>
            (?!          # local part cannot start with dot
                \.
            )
            (
                         # see: http://en.wikipedia.org/wiki/Email_address#Local_part
                [\-0-9a-zA-Z!#$%&'*+/=?^_`{{|}}~]
            |
                \.
                (?!
                    \.   # local part cannot contain two or more non-separated dots
                )
            )+
            (?<!         # local part cannot end with dot
                \.
            )
        )
        @
        (?P<domain>
            {domain}
        )
        \Z
    '''.format(
        domain=DOMAIN_ASCII_LOWERCASE_STRICT_REGEX.pattern.lstrip(' \\A\r\n').rstrip(' \\Z\r\n'),
    ), re.VERBOSE)

# search()-only regexes of source code path prefixes that do not include
# any valuable information (so they can be cut off from debug messages)
USELESS_SRC_PATH_PREFIX_REGEXES = (
    re.compile(r'/N6(?:Core|AdminPanel|GridFSMount|Lib|Portal|Push|RestApi|SDK)/(?=n6)'),
    re.compile(r'/[^/]+\.egg/'),
    re.compile(r'/(?:site|dist)-packages/'),
    re.compile(r'/python[2](?:\.\d+)+/'),
    re.compile(r'^/home/\w+/'),
    re.compile(r'^/usr/(?:(?:local/)?lib/)?'),
)


class RsyncFileContextManager(object):

    """
    A context manager that retrieves data using rsync,
    creates a temporary directory in most secure manner possible,
    stores the downloaded file in that directory,
    returns the downloaded file
    and deletes the temporary directory and its contents.

    The user provides rsync option (e.g. '-z'), source link and name of the temporary file.
    """

    def __init__(self, option, source, dest_tmp_file_name="rsynced_data"):
        self._option = option
        self._source = source
        self._file_name = dest_tmp_file_name
        self._dir_name = None
        self._file = None

    def __enter__(self):
        if self._file is not None:
            raise RuntimeError('Context manager {!r} is not reentrant'.format(self))
        self._dir_name = tempfile.mkdtemp()
        try:
            full_file_path = os.path.join(self._dir_name, self._file_name)
            try:
                subprocess.check_output(["rsync", self._option, self._source, full_file_path],
                                        stderr=subprocess.STDOUT)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError('Cannot download source file (CalledProcessError exception '
                                   'message: "{}"; command output: "{}")'
                                   .format(ascii_str(exc), ascii_str(exc.output)))
            self._file = open(full_file_path)
        except:
            try:
                shutil.rmtree(self._dir_name)
            finally:
                self._dir_name = None
            raise
        return self._file

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._file.close()
        finally:
            try:
                shutil.rmtree(self._dir_name)
            finally:
                self._dir_name = None
                self._file = None


class SimpleNamespace(object):

    """
    Provides attribute access to its namespace, as well as a meaningful repr.

    Copied from http://docs.python.org/3.4/library/types.html and adjusted.
    """

    def __init__(*args, **kwargs):
        try:
            # to avoid arg name clash ('self' may be in kwargs)...
            [self] = args
        except ValueError:
            args_length = len(args)
            assert args_length >= 2
            raise TypeError(
                '{.__class__.__name__}.__init__() takes no positional '
                'arguments ({} given)'.format(args[0], args_length - 1))
        self.__dict__.update(kwargs)

    def __repr__(self):
        namespace = self.__dict__
        items = ("{0}={1!r}".format(k, namespace[k])
                 for k in sorted(namespace))
        return "{0}({1})".format(type(self).__name__, ", ".join(items))

    def __eq__(self, other):
        return self.__dict__ == other.__dict__

    def __ne__(self, other):
        return not self == other


class FilePagedSequence(collections.MutableSequence):

    """
    A mutable sequence that reduces memory usage by keeping data as files.

    Under the hood, the sequence is "paged" -- only the current page
    (consisting of a defined number of items) is kept in memory; other
    pages are pickled and saved as temporary files.

    The interface is similar to the built-in list's one, except that:

    * slices are not supported;
    * del, remove(), insert(), reverse() and sort() are not supported;
    * pop() supports only popping the last item -- and works only if the
      argument is specified as -1 or not specified at all;
    * index() accepts only one argument (does not accept the `start` and
      `stop` range limiting arguments);
    * all sequence items must be picklable;
    * the constructor accepts an additional argument: `page_size` --
      being the number of items each page may consist of (its default
      value is 1000);
    * there are additional methods:
      * clear() -- use it instead of `del seq[:]`;
      * close() -- you should call it when you no longer use the sequence
        (it clears the sequence and removes all temporary files);
      * a context-manager (`with`-statement) interface:
        * its __enter__() returns the instance;
        * its __exit__() calls the close() method.

    Unsupported actions raise NotImplementedError.

    Unpicklable items must *not* be used -- consequences of using them are
    undefined (i.e., apart from an exception being raised, the sequence may
    be left in a defective, inconsistent state).

    Temporary directory and files are created leazily -- no disk operations
    are performed at all if all data fit on one page.

    The implementation is *not* thread-safe.

    >>> list(FilePagedSequence())
    []
    >>> list(FilePagedSequence(page_size=3))
    []
    >>> len(FilePagedSequence(page_size=3))
    0
    >>> bool(FilePagedSequence(page_size=3))
    False

    >>> seq = FilePagedSequence([1, 'foo', {'a': None}, ['b']], page_size=3)
    >>> len(seq)
    4
    >>> bool(seq)
    True
    >>> seq[0]
    1
    >>> seq[-1]
    ['b']
    >>> seq[2]
    {'a': None}
    >>> seq[-2]
    {'a': None}
    >>> seq[1]
    'foo'
    >>> len(seq)
    4

    >>> list(seq)
    [1, 'foo', {'a': None}, ['b']]

    >>> seq.append(42.0)
    >>> len(seq)
    5
    >>> list(seq)
    [1, 'foo', {'a': None}, ['b'], 42.0]

    >>> seq.pop()
    42.0
    >>> len(seq)
    4
    >>> list(seq)
    [1, 'foo', {'a': None}, ['b']]

    >>> seq.pop(-1)
    ['b']
    >>> seq.pop()
    {'a': None}
    >>> list(seq)
    [1, 'foo']
    >>> len(seq)
    2

    >>> seq.append(430)
    >>> seq.append(440)
    >>> seq.append(450)
    >>> list(seq)
    [1, 'foo', 430, 440, 450]
    >>> len(seq)
    5

    >>> seq[2] = 43
    >>> seq[3] = 44
    >>> seq[4] = 45
    >>> list(seq)
    [1, 'foo', 43, 44, 45]
    >>> len(seq)
    5

    >>> seq.append(46)
    >>> seq[5]
    46
    >>> seq[-6]
    1
    >>> len(seq)
    6

    >>> seq.append(47)
    >>> list(seq)
    [1, 'foo', 43, 44, 45, 46, 47]
    >>> len(seq)
    7

    >>> seq.pop()
    47
    >>> list(seq)
    [1, 'foo', 43, 44, 45, 46]

    >>> seq.pop(-1)
    46
    >>> list(seq)
    [1, 'foo', 43, 44, 45]

    >>> seq.pop()
    45
    >>> list(seq)
    [1, 'foo', 43, 44]
    >>> len(seq)
    4

    >>> seq.pop()
    44
    >>> seq[-1]
    43
    >>> list(seq)
    [1, 'foo', 43]

    >>> seq.extend(['a', 'b', 'c'])
    >>> list(seq)
    [1, 'foo', 43, 'a', 'b', 'c']
    >>> len(seq)
    6

    >>> seq[0]
    1

    >>> seq[5] = 'CCC'
    >>> seq[5]
    'CCC'
    >>> len(seq)
    6

    >>> seq.append('DDD')
    >>> seq[1]
    'foo'
    >>> seq[-1]
    'DDD'
    >>> list(seq)
    [1, 'foo', 43, 'a', 'b', 'CCC', 'DDD']

    >>> seq.clear()
    >>> list(seq)
    []
    >>> len(seq)
    0
    >>> seq.pop()  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    IndexError

    >>> seq.extend([1, 'foo', {'a': None}, ['b']])
    >>> seq[0]
    1
    >>> seq[-1]
    ['b']
    >>> seq[2]
    {'a': None}
    >>> seq[-2]
    {'a': None}
    >>> seq[1]
    'foo'
    >>> len(seq)
    4

    >>> list(seq)
    [1, 'foo', {'a': None}, ['b']]

    >>> seq.append(42.0)
    >>> list(seq)
    [1, 'foo', {'a': None}, ['b'], 42.0]

    >>> seq.pop()
    42.0
    >>> list(seq)
    [1, 'foo', {'a': None}, ['b']]

    >>> seq.pop(-1)
    ['b']
    >>> seq.pop()
    {'a': None}
    >>> list(seq)
    [1, 'foo']

    >>> seq.append(43)
    >>> seq.append(44)
    >>> seq.append(45)
    >>> list(seq)
    [1, 'foo', 43, 44, 45]

    >>> seq.append(46)
    >>> seq[5]
    46
    >>> seq[4]
    45
    >>> seq[3]
    44
    >>> seq[-6]
    1

    >>> seq.append(47)
    >>> seq[6]
    47
    >>> list(seq)
    [1, 'foo', 43, 44, 45, 46, 47]

    >>> seq.pop()
    47
    >>> seq[5]
    46
    >>> list(seq)
    [1, 'foo', 43, 44, 45, 46]

    >>> seq.append(47)
    >>> seq[-1]
    47
    >>> list(seq)
    [1, 'foo', 43, 44, 45, 46, 47]

    >>> seq.pop()
    47
    >>> seq[-1]
    46
    >>> list(seq)
    [1, 'foo', 43, 44, 45, 46]

    >>> len(seq)
    6

    >>> seq.pop()
    46
    >>> len(seq)
    5

    >>> seq[-1]
    45
    >>> seq[4]
    45
    >>> list(reversed(seq))
    [45, 44, 43, 'foo', 1]
    >>> seq[5]  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    IndexError
    >>> seq[6]  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    IndexError
    >>> seq[7]  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    IndexError
    >>> seq[8]  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    IndexError

    >>> seq[-5]
    1
    >>> list(seq)
    [1, 'foo', 43, 44, 45]
    >>> seq[-6]  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    IndexError
    >>> seq[0]
    1
    >>> seq[-7]  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    IndexError
    >>> seq[-8]  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    IndexError
    >>> seq[-9]  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    IndexError

    >>> osp.exists(seq._dir)
    True
    >>> sorted(os.listdir(seq._dir))
    ['0', '1', '2']
    >>> seq.close()
    >>> list(seq)
    []
    >>> osp.exists(seq._dir)
    False

    >>> seq2 = FilePagedSequence('abc', page_size=3)
    >>> list(seq2)
    ['a', 'b', 'c']
    >>> seq2._filesystem_used()   # all items in current page -> no disk op.
    False
    >>> seq2.extend('d')          # (now page 0 must be saved)
    >>> seq2._filesystem_used()
    True
    >>> osp.exists(seq2._dir)
    True
    >>> sorted(os.listdir(seq2._dir))
    ['0']
    >>> seq2.extend('ef')
    >>> sorted(os.listdir(seq2._dir))
    ['0']
    >>> seq2.extend('g')          # (now page 1 must be saved)
    >>> sorted(os.listdir(seq2._dir))
    ['0', '1']
    >>> seq2[0]                   # (now page 2 must be saved)
    'a'
    >>> sorted(os.listdir(seq2._dir))
    ['0', '1', '2']
    >>> seq2.pop()
    'g'
    >>> sorted(os.listdir(seq2._dir))
    ['0', '1', '2']
    >>> seq2.clear()
    >>> sorted(os.listdir(seq2._dir))
    ['0', '1', '2']
    >>> seq2.close()
    >>> seq2._filesystem_used()
    True
    >>> osp.exists(seq2._dir)
    False
    >>> list(seq2)
    []

    >>> seq3 = FilePagedSequence(page_size=3)
    >>> seq3._filesystem_used()
    False
    >>> seq3.close()
    >>> seq3._filesystem_used()
    False

    >>> with FilePagedSequence(page_size=3) as seq4:
    ...     not seq4._filesystem_used()
    ...     seq4.append(('foo', 1))
    ...     list(seq4) == [('foo', 1)]
    ...     seq4[0] = 'bar', 2
    ...     seq4[0] == ('bar', 2)
    ...     list(seq4) == [('bar', 2)]
    ...     seq4.append({'x'})
    ...     seq4.append({'z': 3})
    ...     list(seq4) == [('bar', 2), {'x'}, {'z': 3}]
    ...     not seq4._filesystem_used()
    ...     seq4.append(['d'])
    ...     seq4._filesystem_used()
    ...     osp.exists(seq4._dir)
    ...     sorted(os.listdir(seq4._dir)) == ['0']
    ...     seq4[2] = {'ZZZ': 333}
    ...     sorted(os.listdir(seq4._dir)) == ['0', '1']
    ...     list(seq4) == [('bar', 2), {'x'}, {'ZZZ': 333}, ['d']]
    ...     osp.exists(seq4._dir)
    ...
    True
    True
    True
    True
    True
    True
    True
    True
    True
    True
    True
    True
    >>> osp.exists(seq4._dir)
    False
    """

    def __init__(self, iterable=(), page_size=1000):
        self._page_size = page_size
        self._cur_len = 0
        self._cur_page_no = None
        self._cur_page_data = []
        self._closed = False
        self.extend(iterable)

    def __len__(self):
        return self._cur_len

    def __getitem__(self, index):
        local_index = self._local_index(index)
        return self._cur_page_data[local_index]

    def __setitem__(self, index, value):
        local_index = self._local_index(index)
        self._cur_page_data[local_index] = value

    def __reversed__(self):
        for i in xrange(len(self) - 1, -1, -1):
            yield self[i]

    def append(self, value):
        page_no, local_index = divmod(self._cur_len, self._page_size)
        if page_no != self._cur_page_no:
            self._switch_to(page_no, new=(local_index == 0))
        self._cur_page_data.append(value)
        self._cur_len += 1

    def pop(self, index=-1):
        if index != -1:
            raise NotImplementedError('popping using index other '
                                      'than -1 is not supported')
        local_index = self._local_index(-1)
        value = self._cur_page_data.pop(local_index)
        self._cur_len -= 1
        return value

    def __delitem__(self, index):
        raise NotImplementedError('random deletion is not supported')

    def insert(self, index, value):
        raise NotImplementedError('random insertion is not supported')

    def reverse(self):
        raise NotImplementedError('in-place reversion is not supported')

    def sort(self, cmp=None, key=None, reverse=None):
        raise NotImplementedError('in-place sorting is not supported')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.close()

    def clear(self):
        self._cur_page_data = []
        self._cur_page_no = None
        self._cur_len = 0

    def close(self):
        if not self._closed:
            self.clear()
            if self._filesystem_used():
                for filename in os.listdir(self._dir):
                    os.remove(osp.join(self._dir, filename))
                os.rmdir(self._dir)
            self._closed = True

    #
    # Non-public stuff

    @reify
    def _dir(self):
        return tempfile.mkdtemp(prefix='n6-FilePagedSequence-tmp')

    def _filesystem_used(self):
        return '_dir' in self.__dict__

    def _local_index(self, index):
        if isinstance(index, slice):
            raise NotImplementedError('slices are not supported')
        if index < 0:
            index = self._cur_len + index
        if 0 <= index < self._cur_len:
            page_no, local_index = divmod(index, self._page_size)
            if page_no != self._cur_page_no:
                self._switch_to(page_no)
            return local_index
        else:
            raise IndexError

    def _switch_to(self, page_no, new=False):
        if self._cur_page_no is not None:
            # save the current page
            with open(self._get_page_filename(self._cur_page_no), 'wb') as f:
                cPickle.dump(self._cur_page_data, f, -1)
        if new:
            # initialize a new page...
            self._cur_page_data = []
        else:
            # load an existing page...
            with open(self._get_page_filename(page_no), 'rb') as f:
                self._cur_page_data = cPickle.load(f)
        # ...and set it as the current one
        self._cur_page_no = page_no

    def _get_page_filename(self, page_no):
        return osp.join(self._dir, str(page_no))

    #
    # Unittest helper (to test a code that makes use of instances of the class)

    @staticmethod
    def _instance_mock():
        from mock import create_autospec

        NOT_IMPLEMENTED_METHODS = (
            '__delitem__', 'remove', 'insert', 'reverse', 'sort')

        GENERIC_LIST_METHODS = (
            '__iter__', '__len__', '__contains__', '__reversed__',
            'index', 'count', 'append', 'extend', '__iadd__')

        #
        # implementation of method side effects

        li = []

        def make_list_method_side_effect(meth):
            meth_obj = getattr(li, meth)
            def side_effect(*args, **kwargs):
                return meth_obj(*args, **kwargs)
            side_effect.__name__ = meth
            return side_effect

        def getitem_side_effect(index):
            if isinstance(index, slice):
                raise NotImplementedError
            return li[index]

        def setitem_side_effect(index, value):
            if isinstance(index, slice):
                raise NotImplementedError
            li[index] = value

        def pop_side_effect(index=-1):
            if index != -1:
                raise NotImplementedError
            return li.pop(index)

        def clear_side_effect():
            del li[:]

        close_side_effect = itertools.chain(
            [clear_side_effect],
            itertools.repeat(lambda: None))

        def enter_side_effect():
            return FilePagedSequence.__enter__.__func__(m)

        def exit_side_effect(*args):
            return FilePagedSequence.__exit__.__func__(m, *args)

        #
        # configuring the actual mock

        m = create_autospec(FilePagedSequence)()

        # for some mysterious reason (a bug in the mock
        # library?) __reversed__ must be set explicitly
        m.__reversed__ = create_autospec(FilePagedSequence.__reversed__)

        for meth in NOT_IMPLEMENTED_METHODS:
            getattr(m, meth).side_effect = NotImplementedError

        for meth in GENERIC_LIST_METHODS:
            getattr(m, meth).side_effect = make_list_method_side_effect(meth)

        m.__getitem__.side_effect = getitem_side_effect
        m.__setitem__.side_effect = setitem_side_effect
        m.pop.side_effect = pop_side_effect
        m.clear.side_effect = clear_side_effect
        m.close.side_effect = close_side_effect
        m.__enter__.side_effect = enter_side_effect
        m.__exit__.side_effect = exit_side_effect

        m._list = li  # (for introspection in unit tests)
        return m


class DictWithSomeHooks(dict):

    """
    A convenient base for some kinds of dict subclasses.

    * It is a real subclass of the built-in `dict` type (contrary to
      some of the other mapping classes defined in this module).

    * You can extend/override the _custom_key_error() instance method in
      your subclasses to customize exceptions raised by
      __getitem__()/__delitem__()/ pop()/popitem() methods.  The
      _custom_key_error() method should take two positional arguments:
      the originally raised KeyError instance and the name of the method
      ('__getitem__' or '__delitem__, or 'pop', or 'popitem'); for
      details, see the standard implementation of _custom_key_error().

    * The class provides a ready-to-use and recursion-proof __repr__()
      -- you can easily customize it by extending/overriding the
      _constructor_args_repr() method in your subclasses (see the
      example below); another option is, of course, to override
      __repr__() completely.

    * The __ne__ () method (the `!=` operator) is already -- for your
      convenience -- implemented as negation of __eq__() (`==`) -- so
      when you will need, in your subclass, to reimplement/extend the
      equality/inequality operations, typically, you will need to
      override/extend only the __eq__() method.

    * Important: you should not replace the __init__() method completely
      in your subclasses -- but only extend it (e.g., using super()).

    * Your custom __init__() can take any arguments, i.e., its signature
      does not need to be compatible with standard dict's __init__()
      (see the example below).

    * Instances of the class support copying: both shallow copying (do
      it by calling the copy() method or the copy() function from the
      `copy` module) and deep copying (do it by calling the deepcopy()
      function from the `copy` module) -- including copying instance
      attributes and including support for recursive mappings.

      Please note, however, that those copying operations are supposed
      to work properly only if the iteritems() and update() methods work
      as expected -- i.e., that iteritems() generates ``(<hashable key>,
      <corresponding value>)`` pairs (one for each item of the mapping)
      and that update() is able to "consume" an input data object being
      an iterable of such pairs or a `dict` instance created from such
      pairs (the order of items should be considered arbitrary -- it is
      *not* guaranteed to be preserved).

    >>> class MyUselessDict(DictWithSomeHooks):
    ...
    ...     def __init__(self, a, b, c=42):
    ...         super(MyUselessDict, self).__init__(b=b)
    ...         self.a = a
    ...         self.c = self['c'] = c
    ...
    ...     # examples of implementation of the two customization hooks:
    ...
    ...     def _constructor_args_repr(self):
    ...         return '<' + repr(sorted(self.items())) + '>'
    ...
    ...     def _custom_key_error(self, key_error, method_name):
    ...         e = super(MyUselessDict, self)._custom_key_error(
    ...                 key_error, method_name)
    ...         return ValueError(*(e.args + (method_name,)))
    ...
    >>> d = MyUselessDict(['A'], {'B': 'BB'})
    >>> isinstance(d, dict) and isinstance(d, MyUselessDict)
    True
    >>> d
    MyUselessDict(<[('b', {'B': 'BB'}), ('c', 42)]>)
    >>> d == {'b': {'B': 'BB'}, 'c': 42}
    True
    >>> d.a
    ['A']
    >>> d.c
    42

    >>> d['a']
    Traceback (most recent call last):
      ...
    ValueError: ('a', '__getitem__')

    >>> 'a' in d
    False
    >>> d['a'] = d.a
    >>> d['a']
    ['A']
    >>> 'a' in d
    True

    >>> d['c']
    42
    >>> del d['c']
    >>> del d['c']
    Traceback (most recent call last):
      ...
    ValueError: ('c', '__delitem__')
    >>> d.pop('c')
    Traceback (most recent call last):
      ...
    ValueError: ('c', 'pop')
    >>> d.pop('c', 'CCC')
    'CCC'
    >>> d == {'a': ['A'], 'b': {'B': 'BB'}}
    True
    >>> bool(d)
    True

    >>> d
    MyUselessDict(<[('a', ['A']), ('b', {'B': 'BB'})]>)
    >>> d.a
    ['A']
    >>> d.c
    42

    >>> d._repr_recur_thread_ids.add('xyz')
    >>> vars(d) == {'a': ['A'], 'c': 42, '_repr_recur_thread_ids': {'xyz'}}
    True

    >>> d_shallowcopy = d.copy()  # the same as copy.copy(d)
    >>> d_shallowcopy
    MyUselessDict(<[('a', ['A']), ('b', {'B': 'BB'})]>)
    >>> d_shallowcopy == d
    True
    >>> d_shallowcopy is d
    False
    >>> d.c == d_shallowcopy.c == 42
    True
    >>> d['a'] == d.a == d_shallowcopy['a'] == d_shallowcopy.a == ['A']
    True
    >>> d['a'] is d.a is d_shallowcopy['a'] is d_shallowcopy.a
    True
    >>> d['b'] == d_shallowcopy['b'] == {'B': 'BB'}
    True
    >>> d['b'] is d_shallowcopy['b']
    True
    >>> (d._repr_recur_thread_ids == set({'xyz'}) and
    ...  d_shallowcopy._repr_recur_thread_ids == set())  # note this!
    True

    >>> d_deepcopy = copy.deepcopy(d)
    >>> d_deepcopy
    MyUselessDict(<[('a', ['A']), ('b', {'B': 'BB'})]>)
    >>> d_deepcopy == d
    True
    >>> d_deepcopy is d
    False
    >>> d.c == d_deepcopy.c == 42
    True
    >>> d['a'] == d.a == d_deepcopy['a'] == d_deepcopy.a == ['A']
    True
    >>> d['a'] is d_deepcopy['a']  # note this
    False
    >>> d['a'] is d_deepcopy.a     # note this
    False
    >>> d.a is d_deepcopy.a        # note this
    False
    >>> d.a is d_deepcopy['a']     # note this
    False
    >>> d['a'] is d.a
    True
    >>> d_deepcopy['a'] is d_deepcopy.a  # note this
    True
    >>> d['b'] == d_deepcopy['b'] == {'B': 'BB'}
    True
    >>> d['b'] is d_deepcopy['b']  # note this
    False
    >>> (d._repr_recur_thread_ids == set({'xyz'}) and
    ...  d_deepcopy._repr_recur_thread_ids == set())   # note this!
    True

    >>> class RecurKey(object):
    ...     def __repr__(self): return 'rr'
    ...     def __hash__(self): return 42
    ...     def __eq__(self, other): return isinstance(other, RecurKey)
    ...     def __ne__(self, other): return not (self == other)
    ...
    >>> recur_key = RecurKey()
    >>> recur_d = copy.deepcopy(d)
    >>> recur_d._repr_recur_thread_ids.add('xyz')
    >>> vars(recur_d) == {'a': ['A'], 'c': 42, '_repr_recur_thread_ids': {'xyz'}}
    True
    >>> recur_d[recur_key] = recur_d
    >>> recur_d['b'] = recur_d.b = recur_d
    >>> recur_d
    MyUselessDict(<[(rr, MyUselessDict(<...>)), ('a', ['A']), ('b', MyUselessDict(<...>))]>)

    >>> recur_d_deepcopy = copy.deepcopy(recur_d)
    >>> recur_d_deepcopy
    MyUselessDict(<[(rr, MyUselessDict(<...>)), ('a', ['A']), ('b', MyUselessDict(<...>))]>)
    >>> recur_d_deepcopy is recur_d
    False
    >>> [dc_recur_key] = [k for k in recur_d_deepcopy if k == recur_key]
    >>> (dc_recur_key == recur_key and
    ...  hash(dc_recur_key) == hash(recur_key))
    True
    >>> dc_recur_key is recur_key
    False
    >>> (recur_d is                                      # note this!
    ...  recur_d[recur_key] is
    ...  recur_d[recur_key][recur_key] is
    ...  recur_d[recur_key][recur_key][recur_key] is
    ...  recur_d[dc_recur_key] is
    ...  recur_d[dc_recur_key][dc_recur_key] is
    ...  recur_d[dc_recur_key][dc_recur_key][dc_recur_key] is
    ...  recur_d[dc_recur_key][recur_key][dc_recur_key] is
    ...  recur_d[recur_key][dc_recur_key][recur_key] is
    ...  recur_d['b'] is
    ...  recur_d['b']['b'] is
    ...  recur_d['b']['b']['b'] is
    ...  recur_d.b is
    ...  recur_d.b.b is
    ...  recur_d.b.b.b)
    True
    >>> (recur_d_deepcopy is                             # note this!
    ...  recur_d_deepcopy[recur_key] is
    ...  recur_d_deepcopy[recur_key][recur_key] is
    ...  recur_d_deepcopy[recur_key][recur_key][recur_key] is
    ...  recur_d_deepcopy[dc_recur_key] is
    ...  recur_d_deepcopy[dc_recur_key][dc_recur_key] is
    ...  recur_d_deepcopy[dc_recur_key][dc_recur_key][dc_recur_key] is
    ...  recur_d_deepcopy[dc_recur_key][recur_key][dc_recur_key] is
    ...  recur_d_deepcopy[recur_key][dc_recur_key][recur_key] is
    ...  recur_d_deepcopy['b'] is
    ...  recur_d_deepcopy['b']['b'] is
    ...  recur_d_deepcopy['b']['b']['b'] is
    ...  recur_d_deepcopy.b is
    ...  recur_d_deepcopy.b.b is
    ...  recur_d_deepcopy.b.b.b)
    True
    >>> recur_d.c == recur_d_deepcopy.c == 42
    True
    >>> (recur_d['a'] == recur_d.a ==
    ...  recur_d_deepcopy['a'] == recur_d_deepcopy.a == ['A'])
    True
    >>> recur_d['a'] is recur_d_deepcopy['a']
    False
    >>> recur_d['a'] is recur_d_deepcopy.a
    False
    >>> recur_d.a is recur_d_deepcopy.a
    False
    >>> recur_d.a is recur_d_deepcopy['a']
    False
    >>> recur_d['a'] is recur_d.a
    True
    >>> recur_d_deepcopy['a'] is recur_d_deepcopy.a
    True
    >>> (recur_d._repr_recur_thread_ids == set({'xyz'}) and
    ...  recur_d_deepcopy._repr_recur_thread_ids == set())
    True

    >>> recur_d_shallowcopy = copy.copy(recur_d)
    >>> recur_d_shallowcopy                               # doctest: +ELLIPSIS
    MyUselessDict(<[(rr, MyUselessDict(<[(rr, MyUselessDict(<...>)), ('a', ...

    >>> recur_d_shallowcopy == recur_d
    True
    >>> recur_d_shallowcopy is recur_d
    False
    >>> [sc_recur_key] = [k for k in recur_d_shallowcopy if k == recur_key]
    >>> sc_recur_key is recur_key
    True
    >>> (recur_d is
    ...  recur_d_shallowcopy[recur_key] is
    ...  recur_d_shallowcopy[recur_key][recur_key] is
    ...  recur_d_shallowcopy[recur_key][recur_key][recur_key] is
    ...  recur_d_shallowcopy['b'] is
    ...  recur_d_shallowcopy['b']['b'] is
    ...  recur_d_shallowcopy['b']['b']['b'] is
    ...  recur_d_shallowcopy.b is
    ...  recur_d_shallowcopy.b.b is
    ...  recur_d_shallowcopy.b.b.b)
    True
    >>> recur_d.c == recur_d_shallowcopy.c == 42
    True
    >>> (recur_d['a'] == recur_d.a ==
    ...  recur_d_shallowcopy['a'] == recur_d_shallowcopy.a == ['A'])
    True
    >>> (recur_d['a'] is recur_d.a is
    ...  recur_d_shallowcopy['a'] is recur_d_shallowcopy.a)
    True
    >>> (recur_d._repr_recur_thread_ids == set({'xyz'}) and
    ...  recur_d_shallowcopy._repr_recur_thread_ids == set())
    True

    >>> sorted([d.popitem(), d.popitem()])
    [('a', ['A']), ('b', {'B': 'BB'})]
    >>> d
    MyUselessDict(<[]>)
    >>> d == {}
    True
    >>> bool(d)
    False

    >>> d.popitem()
    Traceback (most recent call last):
      ...
    KeyError: 'popitem(): dictionary is empty'

    >>> class AnotherWeirdSubclass(DictWithSomeHooks):
    ...     def __eq__(self, other):
    ...         return 'equal' in other
    ...
    >>> d2 = AnotherWeirdSubclass()
    >>> d2 == {}
    False
    >>> d2 != {}
    True
    >>> d2 == ['equal']
    True
    >>> d2 != ['equal']
    False
    """

    def __init__(*args, **kwargs):
        self = args[0]  # to avoid arg name clash ('self' may be in kwargs)...
        super(DictWithSomeHooks, self).__init__(*args[1:], **kwargs)
        self._repr_recur_thread_ids = set()

    @classmethod
    def fromkeys(cls, *args, **kwargs):
        raise NotImplementedError(
            'the fromkeys() class method is not implemented '
            'for the {0.__name__} class', cls)

    def __repr__(self):
        repr_recur_thread_ids = self._repr_recur_thread_ids
        cur_thread_id = thread.get_ident()
        if cur_thread_id in self._repr_recur_thread_ids:
            # recursion detected
            constructor_args_repr = '<...>'
        else:
            try:
                repr_recur_thread_ids.add(cur_thread_id)
                constructor_args_repr = self._constructor_args_repr()
            finally:
                repr_recur_thread_ids.discard(cur_thread_id)
        return '{0.__class__.__name__}({1})'.format(self, constructor_args_repr)

    def __ne__(self, other):
        return not (self == other)

    def __getitem__(self, key):
        try:
            return super(DictWithSomeHooks, self).__getitem__(key)
        except KeyError as key_error:
            raise self._custom_key_error(key_error, '__getitem__')

    def __delitem__(self, key):
        try:
            super(DictWithSomeHooks, self).__delitem__(key)
        except KeyError as key_error:
            raise self._custom_key_error(key_error, '__delitem__')

    def pop(self, *args):
        try:
            return super(DictWithSomeHooks, self).pop(*args)
        except KeyError as key_error:
            raise self._custom_key_error(key_error, 'pop')

    def popitem(self):
        try:
            return super(DictWithSomeHooks, self).popitem()
        except KeyError as key_error:
            raise self._custom_key_error(key_error, 'popitem')

    def copy(self):
        return copy.copy(self)

    def __copy__(self):
        cls = type(self)
        new = cls.__new__(cls)
        new.update(self.iteritems())
        vars(new).update(vars(self))
        new._repr_recur_thread_ids = set()
        return new

    def __deepcopy__(self, memo):
        cls = type(self)
        new = cls.__new__(cls)
        memo[id(self)] = new  # <- needed in case of a recursive mapping
        new.update(copy.deepcopy(dict(self.iteritems()), memo))
        vars(new).update(copy.deepcopy(vars(self), memo))
        new._repr_recur_thread_ids = set()
        return new

    # the overridable/extendable hooks:

    def _constructor_args_repr(self):
        return repr(dict(self.iteritems()))

    def _custom_key_error(self, key_error, method_name):
        if method_name == 'popitem':
            # for popitem() the standard behaviour is mostly the desired one
            raise key_error
        return key_error


## TODO: doc + maybe more tests (now only the CIDict subclass is doc-tested...)
class NormalizedDict(collections.MutableMapping):

    def __init__(*args, **kwargs):
        self = args[0]  # to avoid arg name clash ('self' may be in kwargs)...
        args = args[1:]
        self._mapping = {}
        self.update(*args, **kwargs)

    @abc.abstractmethod
    def normalize_key(self, key):
        return key

    def __getitem__(self, key):
        nkey = self.normalize_key(key)
        return self._mapping[nkey][1]

    def __setitem__(self, key, value):
        nkey = self.normalize_key(key)
        self._mapping[nkey] = (key, value)

    def __delitem__(self, key):
        nkey = self.normalize_key(key)
        del self._mapping[nkey]

    def __repr__(self):
        return '{0.__class__.__name__}({1!r})'.format(self, dict(self.iteritems()))

    def __len__(self):
        return len(self._mapping)

    def __eq__(self, other):
        if isinstance(other, type(self)):
            return (dict(self.iter_normalized_items()) ==
                    dict(other.iter_normalized_items()))
        return super(NormalizedDict, self).__eq__(other)

    def iterkeys(self):
        return itertools.imap(operator.itemgetter(0), self._mapping.itervalues())

    __iter__ = iterkeys

    def itervalues(self):
        return itertools.imap(operator.itemgetter(1), self._mapping.itervalues())

    def iteritems(self):
        return self._mapping.itervalues()

    def iter_normalized_items(self):
        for nkey, (key, value) in self._mapping.iteritems():
            yield nkey, value

    def keys(self):
        return list(self.iterkeys())

    def values(self):
        return list(self.itervalues())

    def items(self):
        return list(self.iteritems())

    def normalized_items(self):
        return list(self.iter_normalized_items())

    def copy(self):
        return type(self)(self)

    def clear(self):
        self._mapping.clear()

    @classmethod
    def fromkeys(cls, seq, value=None):
        return cls(itertools.izip(seq, itertools.repeat(value)))


class CIDict(NormalizedDict):

    """
    A dict that provides case-insensitive key lookup but keeps original keys.

    (Intended to be used with string keys only).

    >>> d = CIDict({'Aa': 1}, B=2)

    >>> d['aa'], d['AA'], d['Aa'], d['aA']
    (1, 1, 1, 1)
    >>> d['b'], d['B']
    (2, 2)
    >>> del d['b']

    >>> d
    CIDict({'Aa': 1})
    >>> d['cC'] = 3
    >>> d['CC'], d['cc']
    (3, 3)

    >>> sorted(d.keys())
    ['Aa', 'cC']
    >>> sorted(d.values())
    [1, 3]
    >>> sorted(d.items())
    [('Aa', 1), ('cC', 3)]

    >>> d['aA'] = 42
    >>> sorted(d.items())
    [('aA', 42), ('cC', 3)]

    >>> d2 = d.copy()
    >>> d == d2
    True
    >>> d is d2
    False
    >>> len(d), len(d2)
    (2, 2)

    >>> d3 = CIDict.fromkeys(['Cc'], 3)
    >>> d != d3
    True

    >>> d.pop('aa')
    42
    >>> d == d2
    False

    >>> bool(d2)
    True
    >>> d2.clear()
    >>> d2
    CIDict({})
    >>> bool(d2)
    False

    >>> d
    CIDict({'cC': 3})
    >>> d3
    CIDict({'Cc': 3})
    >>> d == d3
    True
    >>> d == {'Cc': 3}
    False
    >>> d == {'cC': 3}
    True

    >>> d.setdefault('CC', 42), d3.setdefault('cc', 43)
    (3, 3)
    >>> d
    CIDict({'cC': 3})
    >>> d3
    CIDict({'Cc': 3})

    >>> d.popitem()
    ('cC', 3)
    >>> d3.popitem()
    ('Cc', 3)
    >>> d == d3 == {}
    True

    >>> d.update([('zz', 1), ('ZZ', 1), ('XX', 5)], xx=6)
    >>> sorted(d.iteritems())
    [('ZZ', 1), ('xx', 6)]
    >>> sorted(d.iter_normalized_items())
    [('xx', 6), ('zz', 1)]
    >>> del d['Xx']
    >>> d.normalized_items()
    [('zz', 1)]
    """

    def normalize_key(self, key):
        key = super(CIDict, self).normalize_key(key)
        return key.lower()


class LimitedDict(collections.OrderedDict):

    """
    Ordered dict whose length never exceeds the specified limit.

    To prevent exceeding the limit the oldest items are dropped.

    >>> from collections import OrderedDict
    >>> lo = LimitedDict([('b', 2), ('a', 1)], maxlen=3)
    >>> lo['c'] = 3
    >>> lo == OrderedDict([('b', 2), ('a', 1), ('c', 3)])
    True
    >>> lo['d'] = 4
    >>> lo == OrderedDict([('a', 1), ('c', 3), ('d', 4)])
    True
    >>> lo.update([((1,2,3), 42), (None, True)])
    >>> lo == OrderedDict([('d', 4), ((1,2,3), 42), (None, True)])
    True

    >>> LimitedDict([('b', 2)])  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    TypeError: ...
    """

    def __init__(*args, **kwargs):
        self = args[0]  # to avoid arg name clash ('self' may be in kwargs)...
        args = args[1:]
        try:
            self._maxlen = kwargs.pop('maxlen')
        except KeyError:
            raise TypeError('{0.__class__.__name__}.__init__ needs '
                            'keyword-only argument maxlen'.format(self))
        super(LimitedDict, self).__init__(*args, **kwargs)

    def __repr__(self):
        """
        >>> LimitedDict(maxlen=3)
        LimitedDict(maxlen=3)

        >>> LimitedDict({1: 2}, maxlen=3)
        LimitedDict([(1, 2)], maxlen=3)

        >>> class StrangeBase(collections.OrderedDict):
        ...     def __repr__(self): return 'XXX'
        ...
        >>> class StrangeSubclass(LimitedDict, StrangeBase):
        ...     pass
        ...
        >>> StrangeSubclass({1: 2}, maxlen=3)  # doctest: +ELLIPSIS
        <n6lib.common_helpers.StrangeSubclass object at 0x...>
        """
        s = super(LimitedDict, self).__repr__()
        if s.endswith(')'):
            ending = 'maxlen={})'
            if not s.endswith('()'):
                ending = ', ' + ending
            return s[:-1] + ending.format(self._maxlen)
        else:
            # only if super()'s __repr__ returned something weird
            # (theoretically possible in subclasses)
            return object.__repr__(self)

    def __setitem__(self, key, value):
        super(LimitedDict, self).__setitem__(key, value)
        if len(self) > self._maxlen:
            self.popitem(last=False)

    def copy(self):
        """
        >>> lo = LimitedDict([(1, 2)], maxlen=3)
        >>> lo
        LimitedDict([(1, 2)], maxlen=3)
        >>> lo.copy()
        LimitedDict([(1, 2)], maxlen=3)
        >>> lo == lo.copy()
        True
        >>> lo is lo.copy()
        False
        """
        return self.__class__(self, maxlen=self._maxlen)

    @classmethod
    def fromkeys(cls, iterable, value=None, **kwargs):
        """
        >>> LimitedDict.fromkeys([1,2,3,4], maxlen=3)
        LimitedDict([(2, None), (3, None), (4, None)], maxlen=3)

        >>> LimitedDict.fromkeys([4,3,2,1], value='x', maxlen=3)
        LimitedDict([(3, 'x'), (2, 'x'), (1, 'x')], maxlen=3)

        >>> LimitedDict.fromkeys([   # (maxlen not given)
        ...               1,2,3,4])  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
          ...
        TypeError: ...
        """
        try:
            maxlen = kwargs.pop('maxlen')
        except KeyError:
            raise TypeError('{0.__name__}.fromkeys needs '
                            'keyword-only argument maxlen'.format(cls))
        self = cls(maxlen=maxlen)
        for key in iterable:
            self[key] = value
        return self


class _CacheKey(object):

    def __init__(self, *args):
        self.args = args
        self.args_hash = hash(args)

    def __repr__(self):
        return '{0.__class__.__name__}{0.args!r}'.format(self)

    def __hash__(self):
        return self.args_hash

    def __eq__(self, other):
        return self.args == other

    def __ne__(self, other):
        return not (self == other)


def memoized(func=None,
             expires_after=None,
             max_size=None,
             max_extra_time=30,
             time_func=time.time):
    """
    A simple in-memory-LRU-cache-providing call memoizing decorator.

    Args:
        `func`:
            The decorated function. Typically it is ommited to
            be bound later with the decorator syntax (see the
            examples below).

    Kwargs:
        `expires_after` (default: None):
            Time interval (in seconds) between caching a call
            result and its expiration. If set to None -- there
            is no time-based cache expiration.
        `max_size` (default: None):
            Maximum number of memoized results (formally, this is
            not a strict maximum: some extra cached results can be
            kept a bit longer -- until their keys' weak references
            are garbage-collected -- though it is hardly probable
            under CPython, and even then it would be practically
            harmless). If set to None -- there is no such limit.
        `max_extra_time` (default: 30):
            Maximum for a random number of seconds to be added to
            `expires_after` for a particular cached result. None
            means the same as 0: no extra time.
        `time_func` (default time.time()):
            A function used to determine current time: it should
            return a timestamp as an int or float number (one second
            is assumed to be the unit).

    Note: recursion is not supported (the decorated function raises
    RuntimeError when a recursive call occurs).

    >>> @memoized(expires_after=None, max_size=2)
    ... def add(a, b):
    ...     print 'calculating: {} + {} = ...'.format(a, b)
    ...     return a + b
    ...
    >>> add(1, 2)  # first time: calling the add() function
    calculating: 1 + 2 = ...
    3
    >>> add(1, 2)  # now, getting cached results...
    3
    >>> add(1, 2)
    3
    >>> add(1, 3)
    calculating: 1 + 3 = ...
    4
    >>> add(1, 2)
    3
    >>> add(1, 3)
    4
    >>> add(3, 1)  # exceeding max_size: forgeting for (1, 2)
    calculating: 3 + 1 = ...
    4
    >>> add(1, 3)
    4
    >>> add(3, 1)
    4
    >>> add(1, 2)  # already forgotten (max_size had been exceeded)
    calculating: 1 + 2 = ...
    3
    >>> add(3, 1)
    4

    >>> t = 0
    >>> pseudo_time = lambda: t
    >>> @memoized(expires_after=4, max_extra_time=None, time_func=pseudo_time)
    ... def sub(a, b):
    ...     print 'calculating: {} - {} = ...'.format(a, b)
    ...     return a - b
    ...
    >>> sub(1, 2)
    calculating: 1 - 2 = ...
    -1

    >>> t = 1
    >>> sub(1, 2)
    -1

    >>> t = 2
    >>> sub(2, 1)
    calculating: 2 - 1 = ...
    1

    >>> t = 3
    >>> sub(4, 2)
    calculating: 4 - 2 = ...
    2

    >>> t = 4      # (t reaches `expires_after` for the (1, 2) call result)
    >>> sub(1, 2)  # forgotten
    calculating: 1 - 2 = ...
    -1

    >>> t = 5      # is still memoized
    >>> sub(2, 1)
    1

    >>> t = 6      # is still memoized (+ expiry of the (2, 1) call result)
    >>> sub(4, 2)
    2

    >>> t = 7      # has already been forgotten
    >>> sub(2, 1)
    calculating: 2 - 1 = ...
    1
    >>> sub(1, 2)  # is still memoized...
    -1

    >>> t = 8
    >>> sub(1, 2)  # and forgotten
    calculating: 1 - 2 = ...
    -1

    >>> @memoized(expires_after=None, max_size=2)
    ... def div(a, b):
    ...     print 'calculating: {} / {} = ...'.format(a, b)
    ...     return a / b
    ...
    >>> div(6, 2)
    calculating: 6 / 2 = ...
    3
    >>> div(8, 2)
    calculating: 8 / 2 = ...
    4
    >>> div(15, 3)
    calculating: 15 / 3 = ...
    5
    >>> try: div(7, 0)
    ... except ZeroDivisionError: print 'Uff'
    calculating: 7 / 0 = ...
    Uff
    >>> try: div(7, 0)
    ... except ZeroDivisionError: print 'Uff'
    calculating: 7 / 0 = ...
    Uff
    >>> try: div(7, 0)
    ... except ZeroDivisionError: print 'Uff'
    calculating: 7 / 0 = ...
    Uff
    >>> div(15, 3)
    5
    >>> div(8, 2)
    4
    >>> div(6, 2)
    calculating: 6 / 2 = ...
    3

    >>> @memoized
    ... def recur(n):
    ...     return (recur(n+1) if n <= 1 else n)
    ...
    >>> recur(1)   # doctest: +ELLIPSIS
    Traceback (most recent call last):
      ...
    RuntimeError: recursive calls cannot be memoized (...)
    """
    if func is None:
        return functools.partial(memoized,
                                 expires_after=expires_after,
                                 max_size=max_size,
                                 max_extra_time=max_extra_time,
                                 time_func=time_func)

    NOT_FOUND = object()
    CacheKey = _CacheKey
    CacheRegItem = collections.namedtuple('CacheRegItem', 'key, expiry_time')
    cache_register = collections.deque(maxlen=max_size)
    keys_to_results = weakref.WeakKeyDictionary()
    mutex = threading.RLock()
    recursion_guard = []

    @functools.wraps(func)
    def wrapper(*args):
        with mutex:
            if recursion_guard:
                raise RuntimeError(
                    'recursive calls cannot be memoized ({0!r} appeared '
                    'to be called recursively)'.format(wrapper))
            recursion_guard.append(None)
            try:
                if expires_after is not None:
                    # delete expired items
                    current_time = time_func()
                    while cache_register and cache_register[0].expiry_time <= current_time:
                        key = cache_register.popleft().key
                        del keys_to_results[key]
                key = CacheKey(*args)
                result = keys_to_results.get(key, NOT_FOUND)
                if result is NOT_FOUND:
                    result = keys_to_results[key] = func(*args)
                    expiry_time = (
                        (time_func() + expires_after +
                         random.randint(0, max_extra_time or 0))
                        if expires_after is not None else None)
                    cache_register.append(CacheRegItem(key, expiry_time))
                return result
            finally:
                recursion_guard.pop()

    wrapper.func = func  # making the original function still available
    return wrapper



class DictDeltaKey(collections.namedtuple('DictDeltaKey', ('op', 'key_obj'))):

    """
    The class of special marker keys in dicts returned by make_dict_delta().

    >>> DictDeltaKey('+', 42)
    DictDeltaKey(op='+', key_obj=42)
    >>> DictDeltaKey(op='-', key_obj='foo')
    DictDeltaKey(op='-', key_obj='foo')
    >>> DictDeltaKey('*', 42)
    Traceback (most recent call last):
      ...
    ValueError: `op` must be one of: '+', '-'

    >>> DictDeltaKey('+', 42) == DictDeltaKey('+', 42)
    True
    >>> DictDeltaKey('+', 42) == DictDeltaKey('-', 42)
    False
    >>> DictDeltaKey('+', 42) == ('+', 42)
    False
    >>> ('-', 'foo') != DictDeltaKey('-', 'foo')
    True
    """

    def __new__(cls, op, key_obj):
        if op not in ('+', '-'):
            raise ValueError("`op` must be one of: '+', '-'")
        return super(DictDeltaKey, cls).__new__(cls, op, key_obj)

    def __eq__(self, other):
        if isinstance(other, DictDeltaKey):
            return super(DictDeltaKey, self).__eq__(other)
        return False

    def __ne__(self, other):
        return not (self == other)


def make_dict_delta(dict1, dict2):
    """
    Compare two dicts and produce a "delta dict".

    Here, "delta dict" is just a dict that contains only differing
    items, with their keys wrapped with DictDeltaKey() instances
    (appropriately: DictDeltaKey('-', <key>) or DictDeltaKey('+', <key>)).

    A few simple examples:

    >>> make_dict_delta({}, {}) == {}
    True
    >>> make_dict_delta({42: 'foo'}, {}) == {DictDeltaKey('-', 42): 'foo'}
    True
    >>> make_dict_delta({}, {'bar': 42}) == {DictDeltaKey('+', 'bar'): 42}
    True
    >>> make_dict_delta({'spam': 42}, {'spam': 42}) == {}
    True
    >>> make_dict_delta({'spam': 42}, {'spam': 42L}) == {}
    True
    >>> make_dict_delta({42: 'spam'}, {42L: 'spam'}) == {}
    True
    >>> make_dict_delta({'spam': 42}, {'spam': 'HAM'}) == {
    ...     DictDeltaKey('-', 'spam'): 42,
    ...     DictDeltaKey('+', 'spam'): 'HAM'}
    True
    >>> make_dict_delta({'spam': 42}, {'HAM': 'spam'}) == {
    ...     DictDeltaKey('-', 'spam'): 42,
    ...     DictDeltaKey('+', 'HAM'): 'spam'}
    True
    >>> delta = make_dict_delta(
    ...     {u'a': 1, u'b': 2L, 'c': 3, 'd': 4L},
    ...     {'b': 2, u'c': 3L, 'd': 42, 'e': 555})
    >>> delta == {
    ...     DictDeltaKey('-', u'a'): 1,
    ...     DictDeltaKey('-', 'd'): 4L,
    ...     DictDeltaKey('+', 'd'): 42,
    ...     DictDeltaKey('+', 'e'): 555}
    True
    >>> delta.pop(DictDeltaKey('+', 'e'))
    555
    >>> delta.pop(DictDeltaKey('+', 'd'))
    42
    >>> delta.pop(DictDeltaKey('-', 'd'))
    4L
    >>> delta.popitem()
    (DictDeltaKey(op='-', key_obj=u'a'), 1)

    Important feature: nested deltas are supported as well.
    For example:

    >>> delta = make_dict_delta({
    ...         'q': 'foo',
    ...         'w': 42,
    ...         'e': ['spam', {42: 'spam'}, 'spam'],
    ...         'r': {
    ...             3: 3L,
    ...             2: {
    ...                 'a': {
    ...                     'aa': 42,
    ...                     'YYY': 'blablabla',
    ...                 },
    ...                 'b': 'bb',
    ...                 'c': {'cc': {'ccc': 43}},
    ...                 'd': {'dd': {'ddd': 44}},
    ...                 'z': {'zz': {'zzz': 123L}},
    ...             },
    ...             1L: 7L,
    ...         },
    ...         't': {'a': 42},
    ...         'y': {},
    ...         'i': 42,
    ...         'o': {'b': 43, 'c': {'d': {'e': 456}}},
    ...         'p': {'b': 43, 'c': {'d': {'e': 456}}},
    ...     }, {
    ...         'q': 'foo',
    ...         u'w': 42L,
    ...         'e': ['spam', {42: 'HAM'}, 'spam'],
    ...         'r': {
    ...             3: 3L,
    ...             2: {
    ...                 'a': {'aa': 43},
    ...                 'b': 'bb',
    ...                 'c': {'cc': {'CCC': 43}},
    ...                 'e': {'ee': {'eee': 45}},
    ...                 'z': {
    ...                     'zz': {u'zzz': 123},
    ...                     'xx': ['bar'],
    ...                 },
    ...             },
    ...             1: 777,
    ...         },
    ...         't': 42,
    ...         'y': {},
    ...         'i': {'a': 42},
    ...         'o': {'b': 43, 'c': {'d': {'e': 456}}},
    ...         'p': {'b': 43, 'c': {'d': {'e': 456789}}},
    ...     })
    >>> delta == {
    ...     DictDeltaKey('-', 'e'): ['spam', {42: 'spam'}, 'spam'],
    ...     DictDeltaKey('+', 'e'): ['spam', {42: 'HAM'}, 'spam'],
    ...     'r': {
    ...         2: {
    ...             'a': {
    ...                 DictDeltaKey('-', 'aa'): 42,
    ...                 DictDeltaKey('+', 'aa'): 43,
    ...                 DictDeltaKey('-', 'YYY'): 'blablabla',
    ...             },
    ...             'c': {
    ...                 'cc': {
    ...                     DictDeltaKey('-', 'ccc'): 43,
    ...                     DictDeltaKey('+', 'CCC'): 43,
    ...                 },
    ...             },
    ...             DictDeltaKey('-', 'd'): {'dd': {'ddd': 44}},
    ...             DictDeltaKey('+', 'e'): {'ee': {'eee': 45}},
    ...             'z': {
    ...                 DictDeltaKey('+', 'xx'): ['bar'],
    ...             },
    ...         },
    ...         DictDeltaKey('-', 1): 7L,
    ...         DictDeltaKey('+', 1): 777,
    ...     },
    ...     DictDeltaKey('-', 't'): {'a': 42},
    ...     DictDeltaKey('+', 't'): 42,
    ...     DictDeltaKey('-', 'i'): 42,
    ...     DictDeltaKey('+', 'i'): {'a': 42},
    ...     'p': {
    ...         'c': {
    ...             'd': {
    ...                 DictDeltaKey('-', 'e'): 456,
    ...                 DictDeltaKey('+', 'e'): 456789,
    ...             },
    ...         },
    ...     },
    ... }
    True
    >>> sorted(delta['r'])  # (a corner case detail: note that within both
    ...                     # DictDeltaKey instances `key_obj` is 1, not 1L)
    [2, DictDeltaKey(op='+', key_obj=1), DictDeltaKey(op='-', key_obj=1)]

    Making deltas from dicts that already contain keys being
    DictDeltaKey instances is (consciously) unsupported:

    >>> make_dict_delta(                 # doctest: +IGNORE_EXCEPTION_DETAIL
    ...     {1: 42},
    ...     {DictDeltaKey('+', 1): 43})
    Traceback (most recent call last):
      ...
    TypeError: ...
    >>> make_dict_delta(                 # doctest: +IGNORE_EXCEPTION_DETAIL
    ...     {'a': {'b': {'c': {DictDeltaKey('-', 'd'): 42}}}},
    ...     {'a': {'b': {'c': {'d': 43}}}})
    Traceback (most recent call last):
      ...
    TypeError: ...
    """
    keys1 = set(dict1)
    keys2 = set(dict2)
    common_keys = keys1 & keys2
    del_keys = keys1 - common_keys
    add_keys = keys2 - common_keys
    if any(isinstance(key, DictDeltaKey)
           for key in itertools.chain(common_keys, del_keys, add_keys)):
        raise TypeError(
            'make_dict_delta() does not accept dicts that '
            'already contain keys being DictDeltaKey instances'
            '(keys of given dicts: {0!r} and {1!r})'.format(
                sorted(dict1), sorted(dict2)))
    delta = {}
    for key in common_keys:
        val1 = dict1[key]
        val2 = dict2[key]
        if isinstance(val1, dict) and isinstance(val2, dict):
            subdelta = make_dict_delta(val1, val2)  # recursion
            if subdelta:
                delta[key] = subdelta
            else:
                assert val1 == val2
        elif val1 != val2:
            del_keys.add(key)
            add_keys.add(key)
    for key in del_keys:
        delta[DictDeltaKey('-', key)] = dict1[key]
    for key in add_keys:
        delta[DictDeltaKey('+', key)] = dict2[key]
    return delta


def deep_copying_result(func):
    """
    A decorator which ensures that the result of each call is deep-copied.

    >>> @deep_copying_result
    ... def func(obj):
    ...     global obj_itself
    ...     obj_itself = obj
    ...     return obj
    ...
    >>> a = [1, 2, {'x': {'y': []}}]
    >>> b = func(a)

    >>> a is b
    False
    >>> a[2] is b[2]
    False
    >>> a[2]['x'] is b[2]['x']
    False
    >>> a[2]['x']['y'] is b[2]['x']['y']
    False

    >>> b == a == obj_itself
    True
    >>> a is obj_itself
    True
    """

    deepcopy = copy.deepcopy

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        orig_result = func(*args, **kwargs)
        return deepcopy(orig_result)

    wrapper.func = func  # making the original function still available
    return wrapper


def exiting_on_exception(func):
    """
    A decorator which ensures that any exception not being SystemExit or
    KeyboardInterrupt will be transformed to SystemExit (instantiated
    with appropriate debug information as the argument).
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (SystemExit, KeyboardInterrupt):
            raise
        except:
            exc_info = sys.exc_info()
            try:
                debug_msg = make_condensed_debug_msg(
                    exc_info,
                    total_limit=None,
                    exc_str_limit=1000,
                    tb_str_limit=None,
                    stack_str_limit=None)
                tb_msg = ''.join(traceback.format_exception(*exc_info)).strip()
                cur_thread = threading.current_thread()
                sys.exit(
                    'FATAL ERROR!\n'
                    '{0}\n'
                    'CONDENSED DEBUG INFO: [thread {1!r} (#{2})] {3}'.format(
                        tb_msg,
                        ascii_str(cur_thread.name),
                        cur_thread.ident,
                        debug_msg))
            finally:
                # (to break any traceback-related reference cycles)
                del exc_info

    wrapper.func = func  # making the original function still available
    return wrapper


def picklable(func_or_class):
    """
    Make the given (possibly non-top-level) function or class picklable.

    Note: this decorator may change values of the `__module__` and/or
    `__name__` attributes of the given function or class.

    >>> from cPickle import PicklingError, dumps, loads
    >>> def _make_nontoplevel():
    ...     def func_a(x, y): return x, y
    ...     func_b = lambda: None
    ...     func_b2 = lambda: None
    ...     func_b3 = lambda: None
    ...     class class_C(object): z = 3
    ...     return func_a, func_b, func_b2, func_b3, class_C
    ...
    >>> a, b, b2, b3, C = _make_nontoplevel()

    >>> a.__module__
    'n6lib.common_helpers'
    >>> a.__name__
    'func_a'
    >>> try: dumps(a)
    ... except (PicklingError, TypeError): print 'Nie da rady!'
    ...
    Nie da rady!
    >>> picklable(a) is a    # applying the decorator
    True
    >>> a.__module__
    'n6lib._picklable_objs'
    >>> a.__name__
    'func_a'
    >>> import n6lib._picklable_objs
    >>> n6lib._picklable_objs.func_a is a
    True
    >>> loads(dumps(a)) is a
    True

    >>> b is not b2 and b is not b3 and b2 is not b3
    True
    >>> (b.__module__ == b2.__module__ == b3.__module__ ==
    ...  'n6lib.common_helpers')
    True
    >>> b.__name__ == b2.__name__ == b3.__name__ == '<lambda>'
    True
    >>> try: dumps(b)
    ... except (PicklingError, TypeError): print 'Nie da rady!'
    ...
    Nie da rady!
    >>> try: dumps(b2)
    ... except (PicklingError, TypeError): print 'Nie da rady!'
    ...
    Nie da rady!
    >>> try: dumps(b3)
    ... except (PicklingError, TypeError): print 'Nie da rady!'
    ...
    Nie da rady!
    >>> picklable(b) is b    # applying the decorator
    True
    >>> picklable(b2) is b2  # applying the decorator
    True
    >>> picklable(b3) is b3  # applying the decorator
    True
    >>> (b.__module__ == b2.__module__ == b3.__module__ ==
    ...  'n6lib._picklable_objs')
    True
    >>> b.__name__
    '<lambda>'
    >>> b2.__name__          # note this value!
    '<lambda>__2'
    >>> b3.__name__          # note this value!
    '<lambda>__3'
    >>> getattr(n6lib._picklable_objs, '<lambda>') is b
    True
    >>> getattr(n6lib._picklable_objs, '<lambda>__2') is b2
    True
    >>> getattr(n6lib._picklable_objs, '<lambda>__3') is b3
    True
    >>> loads(dumps(b)) is b
    True
    >>> loads(dumps(b2)) is b2
    True
    >>> loads(dumps(b3)) is b3
    True

    >>> C.__module__
    'n6lib.common_helpers'
    >>> C.__name__
    'class_C'
    >>> try: dumps(C)
    ... except (PicklingError, TypeError): print 'Nie da rady!'
    ...
    Nie da rady!
    >>> picklable(C) is C    # applying the decorator
    True
    >>> C.__module__
    'n6lib._picklable_objs'
    >>> C.__name__
    'class_C'
    >>> n6lib._picklable_objs.class_C is C
    True
    >>> loads(dumps(C)) is C
    True

    >>> picklable.__module__
    'n6lib.common_helpers'
    >>> picklable.__name__
    'picklable'
    >>> loads(dumps(picklable)) is picklable
    True
    >>> picklable(picklable) is picklable   # nothing changes after applying:
    True
    >>> picklable.__module__
    'n6lib.common_helpers'
    >>> picklable.__name__
    'picklable'
    >>> loads(dumps(picklable)) is picklable
    True
    """
    import importlib
    name = func_or_class.__name__
    try:
        mod = importlib.import_module(func_or_class.__module__)
        if getattr(mod, name, None) is not func_or_class:
            raise ImportError
    except ImportError:
        from n6lib import _picklable_objs
        namespace = vars(_picklable_objs)
        count = 1
        while namespace.setdefault(name, func_or_class) is not func_or_class:
            count += 1
            name = '{}__{}'.format(func_or_class.__name__, count)
        func_or_class.__name__ = name
        func_or_class.__module__ = 'n6lib._picklable_objs'
    return func_or_class


def reduce_indent(a_string):
    r"""
    Reduce indents, retaining relative indentation (ignore first line indent).

    Args:
        `a_string` (str or unicode):
            The string to be modified.

    Returns:
        The input string with minimized indentation (of course, it's a
        new string object); its type is the type of `a_string`.

        Note #1: All tab characters ('\t') are, at first, converted to
        spaces (by applying the expandtabs() method to the input
        string).

        Note #2: The splitlines() method is applied to the input string.
        It means that, in particular, different newline styles are
        recognized ('\n', '\r' and '\r\n') but in the returned string
        all newlines are normalized to '\n' (the Unix style).

        Note #3: The first line as well as any lines that consist only
        of whitespace characters -- are:

        * omitted when it comes to inspection and reduction of
          indentation depth;

        * treated (individually) with the lstrip() string method (so any
          indentation is unconditionally removed from them).

        The remaining lines are subject of uniform reduction of
        indentation -- as deep as possible without changing indentation
        differences between the lines.


    A few examples (including some corner cases):

    >>> reduce_indent(''' Lecz
    ...   Nie za bardzo.
    ...     Za bardzo nie.
    ...       Raczej też.''') == ('''Lecz
    ... Nie za bardzo.
    ...   Za bardzo nie.
    ...     Raczej też.''')
    True

    >>> reduce_indent(u'''\tAzaliż
    ...      Ala ma kota.
    ...       A kot ma Alę.
    ...     Ala go kocha...
    ...
    ... \tA kot na to:
    ...         niemożliwe.
    ... ''') == (u'''Azaliż
    ...  Ala ma kota.
    ...   A kot ma Alę.
    ... Ala go kocha...
    ...
    ...     A kot na to:
    ...     niemożliwe.
    ... ''')
    True

    >>> reduce_indent('''
    ...      Ala ma kota.
    ...       A kot ma Alę.\r
    ...    \vAla go kocha...
    ... \t\f
    ...  \tA kot na to:\f
    ...         niemożliwe.\v
    ... ''') == ('''
    ...  Ala ma kota.
    ...   A kot ma Alę.
    ... Ala go kocha...
    ...
    ...     A kot na to:\f
    ...     niemożliwe.\v
    ... ''')
    True

    >>> reduce_indent('\n \n X\n  ABC')
    '\n\nX\n ABC'
    >>> reduce_indent(u' ---\n \n\t\n  ABC\n\r\n')
    u'---\n\n\nABC\n\n'
    >>> reduce_indent('  abc\t\n    def\r\n   123\r        ')
    'abc   \n def\n123\n'
    >>> reduce_indent(u'    abc\n    def\r\n   123\r        x ')
    u'abc\n def\n123\n     x '

    >>> reduce_indent(u'')
    u''
    >>> reduce_indent(' ')
    ''
    >>> reduce_indent(u'\n')
    u'\n'
    >>> reduce_indent('\r\n')
    '\n'
    >>> reduce_indent(u'\n \n')
    u'\n\n'
    >>> reduce_indent(' \r \r\n ')
    '\n\n'
    >>> reduce_indent(u'x')
    u'x'
    >>> reduce_indent(' x')
    'x'
    >>> reduce_indent(u'\nx\n')
    u'\nx\n'
    >>> reduce_indent(' \r x\r\n ')
    '\nx\n'
    >>> reduce_indent(' \r  x\r\n y\n ')
    '\n x\ny\n'
    """

    _INFINITE_INDENT = float('inf')

    def _get_lines(a_string):
        lines = a_string.expandtabs(8).splitlines()
        assert a_string and lines
        if a_string.endswith(('\n', '\r')):
            lines.append('')
        return lines

    def _get_min_indent(lines):
        min_indent = _INFINITE_INDENT
        for i, li in enumerate(lines):
            lstripped = li.lstrip()
            if lstripped and i > 0:
                cur_indent = len(li) - len(lstripped)
                min_indent = min(min_indent, cur_indent)
        return min_indent

    def _modify_lines(lines, min_indent):
        for i, li in enumerate(lines):
            lstripped = li.lstrip()
            if lstripped and i > 0:
                assert min_indent < _INFINITE_INDENT
                lines[i] = li[min_indent:]
            else:
                lines[i] = lstripped

    if not a_string:
        return a_string
    lines = _get_lines(a_string)
    min_indent = _get_min_indent(lines)
    _modify_lines(lines, min_indent)
    return '\n'.join(lines)


def concat_reducing_indent(*strings):
    r"""
    Concatenate given strings, first applying reduce_indent() to each of them.

    >>> s1 = '''
    ...      1-indented
    ...     zero-indented
    ...                   '''
    >>> s2 = '''Zero-indented
    ...   ZERO-indented
    ...      3-indented
    ...
    ... '''
    >>> s3 = '''  zeRO-indented
    ...               2-indented
    ...                                   \t
    ...             ZeRo-indented
    ...  \t
    ...              1-indented'''
    >>> concat_reducing_indent(s1) == '''
    ...  1-indented
    ... zero-indented
    ... '''
    True
    >>> concat_reducing_indent(s1, s2) == '''
    ...  1-indented
    ... zero-indented
    ... Zero-indented
    ... ZERO-indented
    ...    3-indented
    ...
    ... '''
    True
    >>> concat_reducing_indent(s1, s2, s3) == '''
    ...  1-indented
    ... zero-indented
    ... Zero-indented
    ... ZERO-indented
    ...    3-indented
    ...
    ... zeRO-indented
    ...   2-indented
    ...
    ... ZeRo-indented
    ...
    ...  1-indented'''
    True
    >>> concat_reducing_indent(s3, s2, s1) == '''zeRO-indented
    ...   2-indented
    ...
    ... ZeRo-indented
    ...
    ...  1-indentedZero-indented
    ... ZERO-indented
    ...    3-indented
    ...
    ...
    ...  1-indented
    ... zero-indented
    ... '''
    True
    >>> concat_reducing_indent()
    ''
    >>> concat_reducing_indent('')
    ''
    >>> concat_reducing_indent(u'')
    u''
    >>> concat_reducing_indent(s1, '')
    '\n 1-indented\nzero-indented\n'
    >>> concat_reducing_indent(' ', s1)
    '\n 1-indented\nzero-indented\n'
    >>> concat_reducing_indent(unicode(s1))
    u'\n 1-indented\nzero-indented\n'
    >>> concat_reducing_indent(s1, '   0-indented \t x ')
    '\n 1-indented\nzero-indented\n0-indented    x '
    >>> concat_reducing_indent(unicode(s1), '   0-indented \t x ')
    u'\n 1-indented\nzero-indented\n0-indented    x '
    >>> concat_reducing_indent(s1, u'   0-indented \t x ')
    u'\n 1-indented\nzero-indented\n0-indented    x '
    >>> concat_reducing_indent(unicode(s1), u'   0-indented \t x ')
    u'\n 1-indented\nzero-indented\n0-indented    x '
    >>> concat_reducing_indent(unicode(s1), '', u'   0-indented \t x ')
    u'\n 1-indented\nzero-indented\n0-indented    x '
    """
    return ''.join(map(reduce_indent, strings))


def replace_segment(a_string, segment_index, new_content, sep='.'):
    """
    Replace the specified separator-surrounded segment in the given string.

    Args:
        `a_string`:
            The string to be modified.
        `segment_index`:
            The number (0-indexed) of the segment to be replaced.
        `new_content`:
            The string to be placed as the segment.

    Kwargs:
        `sep` (default: '.'):
            The string that separates segments.

    Returns:
        The modified string -- with the specified segment replaced
        (of course, it's a new string object).

    >>> replace_segment('a.b.c.d', 1, 'ZZZ')
    'a.ZZZ.c.d'
    >>> replace_segment('a::b::c::d', 2, 'ZZZ', sep='::')
    'a::b::ZZZ::d'
    """
    segments = a_string.split(sep)
    segments[segment_index] = new_content
    return sep.join(segments)


def limit_string(s, char_limit, cut_indicator='[...]', middle_cut=False):
    u"""
    Shorten the given string (`s`) to the specified number of characters
    (`char_limit`) by replacing exceeding stuff with the given
    `cut_indicator` ("[...]" by default).

    Note: in this description the term `character` refers to a single
    item of a string (i.e., *single byte* for str strings and *single
    Unicode codepoint* for unicode strings).

    By default, the cut is made at the end of the string but doing it in
    the middle of the string can be requested by specifying `middle_cut`
    as True.

    The `char_limit` number must be greater than or equal to the length
    of the `cut_indicator` string; otherwise ValueError will be raised.

    >>> limit_string('Ala ma kota', 10)
    'Ala m[...]'
    >>> limit_string(u'Alą mą ĸóŧą', 10)
    u'Al\\u0105 m[...]'
    >>> limit_string('Ala ma kota', 11)
    'Ala ma kota'
    >>> limit_string('Ala ma kota', 1000000)
    'Ala ma kota'
    >>> limit_string('Ala ma kota', 9)
    'Ala [...]'
    >>> limit_string('Ala ma kota', 8)
    'Ala[...]'
    >>> limit_string('Ala ma kota', 7)
    'Al[...]'
    >>> limit_string('Ala ma kota', 6)
    'A[...]'
    >>> limit_string('Ala ma kota', 5)
    '[...]'
    >>> limit_string('Ala ma kota', 4)  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...

    >>> limit_string('Ala ma kota', 10, middle_cut=True)
    'Ala[...]ta'
    >>> limit_string(u'Alą mą ĸóŧą', 10, middle_cut=True)
    u'Al\\u0105[...]\\u0167\\u0105'
    >>> limit_string('Ala ma kota', 11, middle_cut=True)
    'Ala ma kota'
    >>> limit_string('Ala ma kota', 1000000, middle_cut=True)
    'Ala ma kota'
    >>> limit_string('Ala ma kota', 9, middle_cut=True)
    'Al[...]ta'
    >>> limit_string('Ala ma kota', 8, middle_cut=True)
    'Al[...]a'
    >>> limit_string('Ala ma kota', 7, middle_cut=True)
    'A[...]a'
    >>> limit_string('Ala ma kota', 6, middle_cut=True)
    'A[...]'
    >>> limit_string('Ala ma kota', 5, middle_cut=True)
    '[...]'
    >>> limit_string('Ala ma kota', 4, middle_cut=True)  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...

    >>> limit_string('Ala ma kota', 10, cut_indicator='****', middle_cut=True)
    'Ala****ota'
    >>> limit_string(u'Alą mą ĸóŧą', 10, cut_indicator='****')
    u'Al\\u0105 m\\u0105****'
    >>> limit_string('Ala ma kota', 6, cut_indicator='****', middle_cut=True)
    'A****a'
    >>> limit_string('Ala ma kota', 5, cut_indicator='****')
    'A****'
    >>> limit_string('Ala ma kota', 4, cut_indicator='****', middle_cut=True)
    '****'
    >>> limit_string('Ala ma kota', 3, cut_indicator='****')  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...

    >>> limit_string(u'Ala ma kota', 0, cut_indicator='')
    u''
    >>> limit_string('Ala ma kota', 0, cut_indicator='', middle_cut=True)
    ''
    >>> limit_string(u'Ala ma kota', 0, cut_indicator='*')  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...

    >>> limit_string('', 10)
    ''
    >>> limit_string(u'', 10, middle_cut=True)
    u''
    >>> limit_string('', 0, cut_indicator='', middle_cut=True)
    ''
    >>> limit_string(u'', 0, cut_indicator='')
    u''
    >>> limit_string('', 0, cut_indicator='*')  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...
    """
    real_limit = char_limit - len(cut_indicator)
    if real_limit < 0:
        raise ValueError(
            '`char_limit` is too small: {0}, i.e., smaller than '
            'the length of `cut_indicator` ({1!r})'.format(
                char_limit,
                cut_indicator))
    if len(s) > char_limit:
        right_limit, odd = divmod(real_limit, 2)
        if middle_cut and right_limit:
            left_limit = right_limit + odd
            s = s[:left_limit] + cut_indicator + s[-right_limit:]
        else:
            s = s[:real_limit] + cut_indicator
    return s


# TODO: doc, tests
### CR: db_event (and maybe some other stuff) uses different implementation
### -- fix it?? (unification needed??)
def ipv4_to_int(ipv4, accept_no_dot=False):
    """
    Return, as int, an IPv4 address specified as a string or integer.

    Args:
        `ipv4`:
            IPv4 as a string (formatted as 4 dot-separated decimal numbers
            or, if `accept_no_dot` is true, as one decimal number) or as
            an int/long number.
        `accept_no_dot` (bool, default: False):
            If true -- accept `ipv4` as a string formatted as one decimal
            number.

    Returns:
        The IPv4 address as an int number.

    Raises:
        ValueError.

    >>> ipv4_to_int('193.59.204.91')
    3241921627
    >>> ipv4_to_int(u'193.59.204.91')
    3241921627
    >>> ipv4_to_int(' 193 . 59 . 204.91')
    3241921627
    >>> ipv4_to_int(u' 193.59. 204 .91 ')
    3241921627
    >>> ipv4_to_int(3241921627)
    3241921627

    >>> ipv4_to_int('3241921627')          # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...

    >>> ipv4_to_int('193.59.204.91.123')   # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...

    >>> ipv4_to_int('193.59.204.256')      # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...

    >>> ipv4_to_int(32419216270000000)     # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...

    >>> ipv4_to_int('3241921627', accept_no_dot=True)
    3241921627
    >>> ipv4_to_int(' 3241921627 ', accept_no_dot=True)
    3241921627
    >>> ipv4_to_int(u'3241921627 ', accept_no_dot=True)
    3241921627

    >>> ipv4_to_int('32419216270000000',   # doctest: +IGNORE_EXCEPTION_DETAIL
    ...             accept_no_dot=True)
    Traceback (most recent call last):
      ...
    ValueError: ...
    """
    try:
        if isinstance(ipv4, (int, long)):
            int_value = ipv4
        elif accept_no_dot and ipv4.strip().isdigit():
            int_value = int(ipv4)
        else:
            numbers = map(int, ipv4.split('.'))  ## FIXME: 04.05.06.0222 etc. are accepted and interpreted as decimal, should they???
            if len(numbers) != 4:
                raise ValueError
            if not all(0 <= num <= 0xff for num in numbers):
                raise ValueError
            multiplied = [num << rot
                          for num, rot in zip(numbers, (24, 16, 8, 0))]
            int_value = sum(multiplied)
        if not 0 <= int_value <= 0xffffffff:
            raise ValueError
    except ValueError:
        raise ValueError('{!r} is not a valid IPv4 address'.format(ipv4))
    return int_value


### CR: db_event (and maybe some other stuff) uses different implementation
### -- fix it?? (unification needed??)
def ipv4_to_str(ipv4, accept_no_dot=False):
    """
    Return, as str, an IPv4 address specified as a string or integer.

    Args:
        `ipv4`:
            IPv4 as a string (formatted as 4 dot-separated decimal numbers
            or, if `accept_no_dot` is true, as one decimal number) or as
            an int/long number.
        `accept_no_dot` (bool, default: False):
            If true -- accept `ipv4` as a string formatted as one decimal
            number.

    Returns:
        The IPv4 address as an str string.

    Raises:
        ValueError.

    >>> ipv4_to_str('193.59.204.91')
    '193.59.204.91'
    >>> ipv4_to_str(u'193.59.204.91')
    '193.59.204.91'
    >>> ipv4_to_str(' 193 . 59 . 204.91')
    '193.59.204.91'
    >>> ipv4_to_str(u' 193.59. 204 .91 ')
    '193.59.204.91'
    >>> ipv4_to_str(3241921627)
    '193.59.204.91'

    >>> ipv4_to_str('3241921627')          # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...

    >>> ipv4_to_str('193.59.204.91.123')   # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...

    >>> ipv4_to_str('193.59.204.256')      # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...

    >>> ipv4_to_str(32419216270000000)     # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...

    >>> ipv4_to_str('3241921627', accept_no_dot=True)
    '193.59.204.91'
    >>> ipv4_to_str(' 3241921627 ', accept_no_dot=True)
    '193.59.204.91'
    >>> ipv4_to_str(u'3241921627 ', accept_no_dot=True)
    '193.59.204.91'

    >>> ipv4_to_str('32419216270000000',   # doctest: +IGNORE_EXCEPTION_DETAIL
    ...             accept_no_dot=True)
    Traceback (most recent call last):
      ...
    ValueError: ...
    """
    int_value = ipv4_to_int(ipv4, accept_no_dot)
    numbers = [(int_value >> rot) & 0xff
               for rot in (24, 16, 8, 0)]
    return '{0}.{1}.{2}.{3}'.format(*numbers)


# maybe TODO later: more tests
def is_ipv4(value):
    """
    Check if the given value is a properly formatted IPv4 address.

    Attrs:
        `value` (str or unicode): the value to be tested.

    Returns:
        Whether the value is properly formatted IPv4 address: True or False.

    >>> is_ipv4('255.127.34.124')
    True
    >>> is_ipv4(u'255.127.34.124')
    True
    >>> is_ipv4('192.168.0.1')
    True
    >>> is_ipv4(' 192.168.0.1 ')
    False
    >>> is_ipv4('192. 168.0.1')
    False
    >>> is_ipv4('192.168.0.0.1')
    False
    >>> is_ipv4('333.127.34.124')
    False
    >>> is_ipv4('3241921627')
    False
    >>> is_ipv4('www.nask.pl')
    False
    >>> is_ipv4(u'www.jaźń\udcdd.pl')
    False
    """
    fields = value.split(".")
    if len(fields) != 4:
        return False
    for value in fields:
        if not (value == value.strip() and (
                value == '0' or value.strip().lstrip('0'))):  ## FIXME: 04.05.06.0333 etc. are accepted, should they???
            return False
        try:
            intvalue = int(value)
        except ValueError:
            return False
        if intvalue > 255 or intvalue < 0:
            return False
    return True


def does_look_like_url(s):
    """
    Check (very roughly) whether the given string looks like an URL.

    It only checks whether the given string startswith some
    letter|digit|dot|plus|minus characters separated with
    a colon from the rest of its contents.

    >>> does_look_like_url('https://www.example.com')
    True
    >>> does_look_like_url('mailto:www.example.com')
    True
    >>> does_look_like_url('foo.bar+spam-you:www.example.com')
    True

    >>> does_look_like_url('www.example.com')
    False
    >>> does_look_like_url('www.example.com/http://foo.bar.pl')
    False
    >>> does_look_like_url('http//www.example.com')
    False
    """
    return (URL_SIMPLE_REGEX.match(s) is not None)


# TODO: more tests
### CR: is it really necessary? consider deleting it... :-/
def safe_eval(node_or_string, namespace=None):
    """
    Copied from Python2.6's ast.literal_eval() + *improved*:

    it also evaluates attribute lookups (if attribute names do not start
    with '_'), based on the given `namespace` dict (if specified).

    >>> safe_eval('1')
    1

    >>> safe_eval('True')
    True

    >>> safe_eval("[1, 2, 3, {4: ('a', 'b', 'c')}]")
    [1, 2, 3, {4: ('a', 'b', 'c')}]

    >>> class C:
    ...     x = 'cherry'
    ...     _private = 'spam'
    >>> class B: C = C
    >>> class A: B = B
    >>> safe_eval('A.B.C.x', {'A': A})
    'cherry'
    >>> safe_eval('A.B.C._private',  # doctest: +IGNORE_EXCEPTION_DETAIL
    ...           {'A': A})
    Traceback (most recent call last):
      ...
    ValueError: ...
    """

    _namespace = {'None': None, 'True': True, 'False': False}
    if namespace is not None:
        _namespace.update(namespace)

    if isinstance(node_or_string, basestring):
        node_or_string = ast.parse(node_or_string, mode='eval')
    if isinstance(node_or_string, ast.Expression):
        node_or_string = node_or_string.body

    def _convert(node):
        if isinstance(node, ast.Str):
            return node.s
        elif isinstance(node, ast.Num):
            return node.n
        elif isinstance(node, ast.Tuple):
            return tuple(map(_convert, node.elts))
        elif isinstance(node, ast.List):
            return list(map(_convert, node.elts))
        elif isinstance(node, ast.Dict):
            return dict((_convert(k), _convert(v)) for k, v
                        in zip(node.keys, node.values))
        elif isinstance(node, ast.Name):
            if node.id in _namespace:
                return _namespace[node.id]
        elif isinstance(node, ast.Attribute):
            parts = _extract_dotted_name_parts(node)
            base = parts[0]
            if base in _namespace:
                return _resolve_dotted_name_parts(_namespace[base], parts[1:])
        raise ValueError('malformed string')

    def _extract_dotted_name_parts(attr_node, parts=()):
        parts = (attr_node.attr,) + parts
        if isinstance(attr_node.value, ast.Attribute):
            return _extract_dotted_name_parts(attr_node.value, parts)
        elif isinstance(attr_node.value, ast.Name):
            return (attr_node.value.id,) + parts
        else:
            raise ValueError('malformed string')

    def _resolve_dotted_name_parts(obj, remaining_parts):
        while remaining_parts:
            attr_name = remaining_parts[0]
            if attr_name.startswith('_'):
                raise ValueError('malformed string (underscored '
                                 'attributes not allowed')
            obj = getattr(obj, attr_name)
            remaining_parts = remaining_parts[1:]
        return obj

    return _convert(node_or_string)


def with_flipped_args(func):
    """
    From a given function that takes exactly two positional parameters
    -- make a new function that takes these parameters in the reversed
    order.

    >>> def foo(first, second):
    ...     print first, second
    ...
    >>> foo(42, 'zzz')
    42 zzz
    >>> flipped_foo = with_flipped_args(foo)
    >>> flipped_foo(42, 'zzz')
    zzz 42
    >>> flipped_foo.__name__
    'foo__with_flipped_args'

    This function can be useful when using functools.partial(), e.g.:

    >>> from functools import partial
    >>> from operator import contains
    >>> is_42_in = partial(with_flipped_args(contains), 42)
    >>> is_42_in([42, 43, 44])
    True
    >>> is_42_in({42: 43})
    True
    >>> is_42_in([1, 2, 3])
    False
    >>> is_42_in({43: 44})
    False
    """
    def flipped_func(a, b):
        return func(b, a)
    flipped_func.__name__ = func.__name__ + '__with_flipped_args'
    return flipped_func


def read_file(name, *open_args):
    """
    Open the file and read its contents.

    Args:
        `name`:
            The file name (path).
        Other positional arguments:
            To be passed into the open() build-in function.
    """
    with open(name, *open_args) as f:
        return f.read()


def make_hex_id(length=96, additional_salt=''):
    """
    Make a random, unpredictable id consisting of hexadecimal digits.

    Args/kwargs:
        `length` (int; default: 96):
            The number of hexadecimal digits the generated id shall
            consist of.  Must not be less than 1 or greater than 96 --
            or ValueError will be raised.
        `additional_salt` (str; default: ''):
            Additional string to be mixed in when generating the id.
            Hardly needed but it does not hurt to specify it. :)

    Returns:
        A str consisting of `length` hexadecimal lowercase digits.

    Raises:
        ValueError -- if `length` is lesser than 1 or greater than 96.
        TypeError -- if `additional_salt` is not a str instance.

    >>> import string; is_hex = set(string.hexdigits.lower()).issuperset
    >>> h = make_hex_id()
    >>> isinstance(h, str) and is_hex(h) and len(h) == 96
    True
    >>> h = make_hex_id(additional_salt='some salt')
    >>> isinstance(h, str) and is_hex(h) and len(h) == 96
    True
    >>> h = make_hex_id(96)
    >>> isinstance(h, str) and is_hex(h) and len(h) == 96
    True
    >>> h = make_hex_id(1, additional_salt='some other salt')
    >>> isinstance(h, str) and is_hex(h) and len(h) == 1
    True
    >>> h = make_hex_id(length=40)
    >>> isinstance(h, str) and is_hex(h) and len(h) == 40
    True

    >>> make_hex_id(0)  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...
    >>> make_hex_id(97)  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...
    >>> make_hex_id(42, additioanl_salt=u'x')  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    TypeError: ...
    >>> make_hex_id(additioanl_salt=42)  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    TypeError: ...
    """
    if not 1 <= length <= 96:
        raise ValueError('`length` must be in the range 1..96 ({0!r} given)'.format(length))
    if not isinstance(additional_salt, str):
        raise TypeError('`additional_salt` must be str, not {0}'.format(
            type(additional_salt).__name__))
    hash_base = os.urandom(40) + additional_salt + '{0:.24f}'.format(time.time())
    hex_id = hashlib.sha384(hash_base).hexdigest()[:length]
    return hex_id


def normalize_hex_id(hex_id, min_digit_num=0):
    """
    Normalize the given `hex_id` string so that the result is a str
    instance, without the '0x prefix, at least `min_digit_num`-long
    (padded with zeroes if necessary; `min_digit_num` defaults to 0)
    and containing only lowercase hexadecimal digits.

    Examples:

    >>> normalize_hex_id('1')
    '1'
    >>> normalize_hex_id('10')
    '10'
    >>> normalize_hex_id('10aBc')
    '10abc'
    >>> normalize_hex_id('0x10aBc')
    '10abc'
    >>> normalize_hex_id('10aBc', 0)
    '10abc'
    >>> normalize_hex_id('10aBc', 4)
    '10abc'
    >>> normalize_hex_id('10aBc', 5)
    '10abc'
    >>> normalize_hex_id('10aBc', 6)
    '010abc'
    >>> normalize_hex_id('0x10aBc', 6)
    '010abc'
    >>> normalize_hex_id('12A4E415E1E1B36FF883D1')
    '12a4e415e1e1b36ff883d1'
    >>> normalize_hex_id('12A4E415E1E1B36FF883D1', 30)
    '0000000012a4e415e1e1b36ff883d1'
    >>> normalize_hex_id('0x12A4E415E1E1B36FF883D1', 30)
    '0000000012a4e415e1e1b36ff883d1'
    >>> normalize_hex_id('')   # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    ValueError: ...
    >>> normalize_hex_id(1)    # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    TypeError: ...
    """
    int_id = int(hex_id, 16)
    return int_id_to_hex(int_id, min_digit_num)


def int_id_to_hex(int_id, min_digit_num=0):
    """
    Convert the given `int_id` integer so that the result is a str
    instance, without the '0x prefix, at least `min_digit_num`-long
    (padded with zeroes if necessary; `min_digit_num` defaults to 0)
    and containing only lowercase hexadecimal digits.

    Examples:

    >>> int_id_to_hex(1)
    '1'
    >>> int_id_to_hex(1L)
    '1'
    >>> int_id_to_hex(31, 0)
    '1f'
    >>> int_id_to_hex(31L, 1)
    '1f'
    >>> int_id_to_hex(31, 2)
    '1f'
    >>> int_id_to_hex(31, 3)
    '01f'
    >>> int_id_to_hex(31, 10)
    '000000001f'
    >>> int_id_to_hex(22539340290692258087863249L)
    '12a4e415e1e1b36ff883d1'
    >>> int_id_to_hex(22539340290692258087863249L, 30)
    '0000000012a4e415e1e1b36ff883d1'
    >>> int_id_to_hex('1')   # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
      ...
    TypeError: ...
    """
    hex_id = hex(int_id)
    assert hex_id[:2] == '0x'
    # get it *without* the '0x' prefix and the 'L' suffix
    # (the integer value could be a long integer)
    hex_id = hex_id[2:].rstrip('L')
    # pad with zeroes if necessary
    hex_id = hex_id.rjust(min_digit_num, '0')
    return hex_id


def cleanup_src():
    """
    Delete all extracted resource files and directories,
    logs a list of the file and directory names that could not be successfully removed.
    [see: https://pythonhosted.org/setuptools/pkg_resources.html#resource-extraction]
    """
    from n6lib.log_helpers import get_logger
    _LOGGER = get_logger(__name__)

    fail_cleanup = cleanup_resources()
    if fail_cleanup:
        _LOGGER.warning('Fail cleanup resources: %r', fail_cleanup)


# maybe TODO later: tests (so far, tested manually)
def make_condensed_debug_msg(exc_info=None,
                             total_limit=2000,
                             exc_str_limit=500,
                             tb_str_limit=1000,
                             stack_str_limit=None,
                             cut_indicator='[...]'):
    """
    Generate a one-line string containing condensed debug information,
    including: script basename, hostname, exception class, exception
    str() representation, condensed traceback info, condensed outer
    stack frames info.

    Args/kwargs:
        `exc_info` (default: None):
            None or a 3-tuple, as returned by sys.exc_info().  If None:
            sys.exc_info() will be called to obtain the exception
            information.

    Kwargs:
        `total_limit` (default: 2000):
            Maximum length of the resultant str.  If None: no limit.
        `exc_str_limit` (default: 500):
            Maximum length of the exception str() representation part of
            the resultant str.  If None: no limit.
        `tb_str_limit` (default: 1000):
            Maximum length of the traceback part of the resultant str.
            If None: no limit.
        `stack_str_limit` (default: None):
            Maximum length of the outer stack frames info part of the
            resultant str.  If None: no limit.
        `cut_indicator` (default: "[...]"):
            The string that will replace cut fragments.  It should be a
            pure ASCII str (if not it will be automatically converted to
            such a str).

    Returns:
        The resultant debug info (being a pure ASCII str).

    Raises:
        ValueError -- if any of the `*_limit` arguments is smaller than
        the length of the string specified as `cut_indicator`.
    """
    try:
        def format_entry(entry_tuple):
            filename, lineno, funcname, codequote = entry_tuple
            if not filename:
                filename = '<unknown file>'
            for useless_prefix_regex in USELESS_SRC_PATH_PREFIX_REGEXES:
                match = useless_prefix_regex.search(filename)
                if match:
                    filename = filename[match.end(0):]
                    break
            s = filename
            if lineno:
                s = '{0}#{1}'.format(s, lineno)
            if funcname and funcname != '<module>':
                s = '{0}/{1}()'.format(s, funcname)
            if codequote:
                s = '{0}:`{1}`'.format(s, codequote)
            return s

        def make_msg(obj, limit, middle_cut=True):
            if obj is None:
                return ''
            if isinstance(obj, type):
                obj = obj.__name__
            s = ascii_str(obj).replace('\n', '\\n').replace('\r', '\\r')
            if limit is not None:
                s = limit_string(s, limit, cut_indicator, middle_cut)
            return s

        cut_indicator = ascii_str(cut_indicator)
        if exc_info is None:
            exc_info = sys.exc_info()
        exc_type, exc, tb = exc_info

        if tb is None:
            tb_formatted = None
            stack_entry_tuples = traceback.extract_stack()[:-1]
        else:
            tb_entry_tuples = traceback.extract_tb(tb)
            tb_formatted = ' <- '.join(map(
                format_entry,
                reversed(tb_entry_tuples)))
            stack_entry_tuples = traceback.extract_stack(tb.tb_frame)
        stack_formatted = ' <- '.join(map(
            format_entry,
            reversed(stack_entry_tuples)))

        full_msg = '[{0}@{1}] {2}: {3} <<= {4} <-(*)- {5}'.format(
            SCRIPT_BASENAME,
            HOSTNAME,
            make_msg(exc_type, 100) or '<no exc>',
            make_msg(exc, exc_str_limit) or '<no msg>',
            make_msg(tb_formatted, tb_str_limit) or '<no traceback>',
            make_msg(stack_formatted, stack_str_limit))
        return make_msg(full_msg, total_limit, middle_cut=False)

    finally:
        # (to break any traceback-related reference cycles)
        exc_info = exc_type = exc = tb = None


_dump_condensed_debug_msg_lock = threading.RLock()

def dump_condensed_debug_msg(header=None, stream=None):
    """
    Call make_condensed_debug_msg(total_limit=None, exc_str_limit=1000,
    tb_str_limit=None, stack_str_limit=None) and print the resultant
    debug message (adding to it an apropriate caption, containing
    current thread's identifier and name, and optionally preceding it
    with the specified `header`) to the standard error output or to the
    specified `stream`.

    This function is thread-safe (guarded with an RLock).

    Args/kwargs:
        `header` (default: None):
            Optional header -- to be printed above the actual debug
            information.
        `stream` (default: None):
            The stream the debug message is to be printed to.  If None
            the message will be printed to the standard error output.
    """
    with _dump_condensed_debug_msg_lock:
        header = (
            '\n{0}\n'.format(ascii_str(header)) if header is not None
            else '')
        if stream is None:
            stream = sys.stderr
        debug_msg = make_condensed_debug_msg(
            total_limit=None,
            exc_str_limit=1000,
            tb_str_limit=None,
            stack_str_limit=None)
        cur_thread = threading.current_thread()
        print >>stream, '{0}\nCONDENSED DEBUG INFO: [thread {1!r} (#{2})] {3}\n'.format(
            header,
            ascii_str(cur_thread.name),
            cur_thread.ident,
            debug_msg)
        try:
            stream.flush()
        except Exception:
            pass


if __name__ == '__main__':
    from n6lib.unit_test_helpers import run_module_doctests
    run_module_doctests()
