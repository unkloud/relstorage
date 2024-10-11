##############################################################################
#
# Copyright (c) 2008 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""The core of RelStorage, a ZODB storage for relational databases.

Stores pickles in the database.
"""
from __future__ import absolute_import
from __future__ import print_function

import logging
import sys
import weakref

from transaction.interfaces import TransientError
import ZODB.interfaces


from ZODB import ConflictResolution

from ZODB.mvccadapter import HistoricalStorageAdapter

from ZODB.POSException import ReadConflictError
from ZODB.POSException import ReadOnlyError
from ZODB.POSException import ReadOnlyHistoryError

from ZODB.utils import z64
from zope import interface
from zope.interface import implementer

from ..blobhelper import BlobHelper
from ..blobhelper.interfaces import IBlobHelper
from ..blobhelper.interfaces import INoBlobHelper

from ..cache.storage_cache import StorageCache
from ..cache.interfaces import CacheConsistencyError
from ..options import Options
from ..interfaces import IRelStorage
from ..adapters.connections import LoadConnection
from ..adapters.connections import StoreConnectionPool
from ..adapters.connections import ClosedConnection
from ..adapters.connections import ClosedConnectionPool
from ..adapters.connections import SingleConnectionPool
from .._compat import clear_frames
from .._util import metricmethod
from .._util import int64_to_8bytes
from .._util import bytes8_to_int64
from .._compat import OID_SET_TYPE

from .transaction_iterator import HistoryFreeTransactionIterator
from .transaction_iterator import HistoryPreservingTransactionIterator

from .copy import Copy
from .history import History
from .history import UndoableHistory
from .legacy import LegacyMethodsMixin
from .load import Loader
from .load import BlobLoader
from .oid import OIDs
from .oid import ReadOnlyOIDs
from .pack import Pack
from .store import Storer
from .store import BlobStorer

from .tpc import NotInTransaction
from .tpc.begin import HistoryFree
from .tpc.begin import HistoryPreserving
from .tpc.restore import Restore

from .util import copy_storage_methods
from .util import make_cannot_write
from .interfaces import StorageDisconnectedDuringCommit

__all__ = [
    'RelStorage',
]

log = logger = logging.getLogger("relstorage")


class _ClosedCache(object):
    __slots__ = ()

    def close(self):
        "does nothing"

    release = close
    object_index = ()
    highest_visible_tid = None
    stats = lambda s: {'closed': True}
    afterCompletion = lambda s, c: None


@implementer(IRelStorage)
class RelStorage(LegacyMethodsMixin,
                 ConflictResolution.ConflictResolvingStorage):

    """
    Storage to a relational database, based on invalidation polling.
    """

    # pylint:disable=too-many-instance-attributes,too-many-public-methods

    _adapter = None
    _options = None
    _is_read_only = False
    _read_only_error = ReadOnlyError
    # ZODB TID of the last transaction committed by this instance.
    _last_tid_i_committed_bytes = z64

    # _closed is True after self.close() is called.
    _closed = False

    # _cache if set is a StorageCache object.
    _cache = None

    # If the blob directory is set, blobhelper is a BlobHelper.
    blobhelper = BlobHelper(None, None)

    # The state of committing that were in. Certain operations are
    # only available in certain states; certain information is only needed
    # in certain states.
    _tpc_phase = None

    __name__ = None

    # _instances is a list of weak references to storage instances bound
    # to the same database.
    _instances = ()

    _load_connection = ClosedConnection()
    _store_connection_pool = ClosedConnectionPool()

    _oids = ReadOnlyOIDs()

    def __init__(self, adapter, name=None, create=None,
                 options=None, cache=None, blobhelper=None,
                 store_connection_pool=None,
                 **kwoptions):
        # pylint:disable=too-many-branches, too-many-statements, too-complex, too-many-locals
        if options and kwoptions:
            raise TypeError("The RelStorage constructor accepts either "
                            "an options parameter or keyword arguments, not both")

        self._adapter = adapter

        if options is None:
            options = Options(**kwoptions)
        self._options = options

        if not name:
            name = options.name
            if not name:
                name = 'RelStorage: %s' % adapter
        self.__name__ = name

        self._is_read_only = options.read_only

        need_check_compat = create is None
        if create is None:
            create = options.create_schema

        if create:
            self._adapter.schema.prepare()
        elif need_check_compat:
            # At the top level, not new_instance(), and not asked to create.
            self._adapter.schema.verify()


        # A ZODB Connection is documented as not being thread-safe and
        # must be used only by a single thread at a time. In IMVCC,
        # each Connection has a unique storage object. So the storage
        # object is used only by a single thread. So there's really
        # not much point in RelStorage keeping thread locks around its
        # methods.

        # There are a small handful of ZODB tests that assume the
        # storage is thread safe (but they know nothing about IMVCC);
        # for those tests we use a custom wrapper storage that makes
        # a single instance appear thread safe.

        self._instances = []

        self._load_connection = LoadConnection(self._adapter.connmanager)
        self._load_connection.on_first_use = self.__on_load_first_use
        self.__queued_changes = OID_SET_TYPE()
        if store_connection_pool is not None:
            self._store_connection_pool = store_connection_pool
        elif not self._is_read_only:
            self._store_connection_pool = StoreConnectionPool(self._adapter.connmanager)

        if cache is not None:
            self._cache = cache
        else:
            prefix = options.cache_prefix
            if not prefix:
                # Use the database name as the cache prefix.
                with self._load_connection.isolated_connection() as cur:
                    prefix = adapter.schema.get_database_name(cur)

                prefix = prefix.replace(' ', '_')
                options.cache_prefix = prefix
            self._cache = StorageCache(adapter, options, prefix)

        if blobhelper is not None:
            self.blobhelper = blobhelper
        else:
            self.blobhelper = BlobHelper(options=options, adapter=adapter)

        tpc_begin_factory = HistoryPreserving if self._options.keep_history else HistoryFree

        if hasattr(self._adapter.packundo, 'deleteObject'):
            interface.alsoProvides(self, ZODB.interfaces.IExternalGC)

        self._tpc_phase = NotInTransaction(tpc_begin_factory, self._is_read_only)
        if not self._is_read_only:
            self._oids = OIDs(self._adapter.oidallocator)

        # Now copy in a bunch of methods from our component objects.
        # Many of these are 'stale_aware', meaning that we can ask
        # them for a version of themselves that does something
        # different when we go stale. When that happens, we *replace*
        # the object in our __dict__ with the new one; and then
        # reverse that when we're no longer stale.
        #
        # The storage wrapper zc.zlibstorage also copies methods into
        # itself when it is created, from the storage it is wrapping.
        # Because of this, stale aware methods like history() do not
        # do the right thing when we're wrapped by zc.zlibstorage.
        loader = Loader(self._adapter, self._load_connection, self._cache)
        copy_storage_methods(self, loader)
        storer = Storer()
        copy_storage_methods(self, storer)

        if options.keep_history:
            interface.alsoProvides(self, ZODB.interfaces.IStorageUndoable)
            history = UndoableHistory(self._adapter, self._load_connection)
        else:
            history = History(self._adapter, self._load_connection)
        copy_storage_methods(self, history)

        assert IBlobHelper.providedBy(self.blobhelper) # pylint:disable=no-value-for-parameter
        if not INoBlobHelper.providedBy(self.blobhelper): # pylint:disable=no-value-for-parameter
            interface.alsoProvides(self, ZODB.interfaces.IBlobStorageRestoreable)

            loader = BlobLoader(self._load_connection, self.blobhelper)
            copy_storage_methods(self, loader)

            storer = BlobStorer()
            copy_storage_methods(self, storer)

    @property
    def keep_history(self):
        return self._options.keep_history

    def __repr__(self):
        return "<%s at 0x%x keep_history=%s phase=%r blobhelper=%r cache=%r>" % (
            self.__class__.__name__,
            id(self),
            self.keep_history,
            self._tpc_phase,
            self.blobhelper,
            self._cache
        )

    def new_instance(self, before=None):
        """
        Creates and returns another storage instance.

        See ZODB.interfaces.IMVCCStorage.
        """
        # pylint:disable=protected-access
        options = self._options
        if before and not self._options.read_only:
            options = self._options.new_instance(read_only=True)
        adapter = self._adapter.new_instance()
        cache = self._cache.new_instance(before=before, adapter=adapter)
        blobhelper = self.blobhelper.new_instance(adapter=adapter)
        other = type(self)(adapter=adapter, name=self.__name__,
                           create=False, options=options, cache=cache,
                           blobhelper=blobhelper,
                           store_connection_pool=self._store_connection_pool.new_instance())
        if before:
            other._read_only_error = ReadOnlyHistoryError
            other.tpc_begin = make_cannot_write(other, other.tpc_begin)
        # NOTE: We're depending on the GIL (or list implementation)
        # for thread safety here.
        self._instances.append(weakref.ref(other, self._instances.remove))

        if self._crs_transform_record_data is not type(self)._crs_transform_record_data:
            # registerDB has been called on us but isn't called on
            # our children. Make sure any wrapper that needs to transform
            # records can do so.
            # See https://github.com/zodb/relstorage/issues/71
            #
            # NOTE: This is *probably* unnecessary now that we directly invoke the constructor
            # for zc.zlibstorage in *its* ``new_instance`` method.
            other._crs_transform_record_data = self._crs_transform_record_data
            other._crs_untransform_record_data = self._crs_untransform_record_data
        return other

    def before_instance(self, before):
        """
        Return a historical connection for the given *before* tid.

        The connection is read-only, and that is enforced at this level.
        """
        # XXX This might need some work to better adapt the cache? We
        # can *know* that we're pinned to see older transactions, so
        # our TID doesn't need to move forward, and we don't need to
        # poll. We're read-only so we'll never be joined to a
        # transaction or commit, we'll never have invalidations. The
        # HistoricalStorageAdapter blocks sync() and
        # poll_invalidations() from being called on this instance, but
        # our load connection is going to do that automatically the
        # first time it's accessed after a transaction
        # rollback...which, since we're not joined to a transaction,
        # should never happen...except for afterCompletion(), which
        # the ZODB connection has called on it for every transaction
        # whether it's joined or not...but the HistoricalStorageAdapter
        # doesn't implement that *either*, so it never makes it down to
        # this level. This means our load connection can stay open and
        # un-rolled-back for quite a long time, which is probably not great.
        i = self.new_instance(before=before)
        x = HistoricalStorageAdapter(i, before)
        return x

    @property
    def highest_visible_tid(self):
        cache_tid = self._cache.highest_visible_tid or 0
        committed_tid = bytes8_to_int64(self._last_tid_i_committed_bytes)
        # In case we haven't polled yet.
        return max(cache_tid, committed_tid)

    def zap_all(self, **kwargs):
        """Clear all objects and transactions out of the database.

        Used by the test suite and the ZODBConvert script.
        """
        self._load_connection.drop()
        self._store_connection_pool.drop_all()
        self._adapter.schema.zap_all(**kwargs)
        self._cache.zap_all()

    def release(self):
        """
        Release external resources used by this storage instance.

        This includes the database sessions (connections) and any memcache
        connections.

        Does *not* affect any other instances created by this instance. This object
        should still be :meth:`close` (but note that might have global affects
        on other instances of the same base object).
        """
        self._load_connection.drop()
        self._store_connection_pool.release()

        self._cache.release()
        self._cache = _ClosedCache()
        self._tpc_phase.close()
        self._tpc_phase = _ClosedCache()
        self._oids = None
        self._load_connection = ClosedConnection()
        self._store_connection_pool = ClosedConnectionPool()
        self._adapter.release()
        if not self._instances:
            self._closed = True

    def close(self):
        """Close the storage and all instances."""
        if self._closed:
            return

        self._closed = True
        self._load_connection.drop()
        self._store_connection_pool.close()
        self._load_connection = ClosedConnection()
        self._store_connection_pool = ClosedConnectionPool()

        self.blobhelper.close()
        for wref in self._instances:
            instance = wref()
            if instance is not None:
                instance.close()
        self._instances = ()
        logger.debug("Closing storage cache with stats %s", self._cache.stats())
        self._cache.close()
        self._cache = _ClosedCache()
        self._tpc_phase.close()
        self._tpc_phase = _ClosedCache()
        self._oids = None
        self._adapter.close()

    def __len__(self):
        return self._adapter.stats.get_object_count()

    def sortKey(self):
        """Return a string that can be used to sort storage instances.

        The key must uniquely identify a storage and must be the same
        across multiple instantiations of the same storage.
        """
        return self.__name__

    def getName(self):
        return self.__name__

    def getSize(self):
        """Return database size in bytes"""
        return self._adapter.stats.get_db_size()

    def registerDB(self, wrapper):
        # pylint:disable=no-value-for-parameter
        if (ZODB.interfaces.IStorageWrapper.providedBy(wrapper)
                # Prior to ZODB 5, this would be called by the database itself.
                # (I wish it would still do that.)
                and not ZODB.interfaces.IDatabase.providedBy(wrapper)
                and not hasattr(type(wrapper), 'new_instance')):
            # Fixes for https://github.com/zopefoundation/zc.zlibstorage/issues/2
            # We special-case zlibstorage because we know that just calling
            # its type won't necessarily do the correct thing.
            if hasattr(wrapper, 'base') and hasattr(wrapper, 'copied_methods'):
                type(wrapper).new_instance = _zlibstorage_new_instance
                type(wrapper).pack = _zlibstorage_pack
                from zc.zlibstorage import _Iterator
                _Iterator.__len__ = _zlibstorage_Iterator_len
            else:
                wrapper.new_instance = lambda s: type(wrapper)(self.new_instance())

        # zc.zlibstorage has a custom copyTransactionsFrom that hides
        # our own implementation. It just uses ZODB.blob.copyTransactionsFromTo.
        # Use our implementation. Note that we don't alter ``copied_methods``,
        # because that would change this for all wrappers in the same process,
        # e.g., around a FileStorage. This should be safe and correct to do for all
        # wrappers because our copy method uses the standard _crs_transform_record_data
        # method.
        wrapper.copyTransactionsFrom = self.copyTransactionsFrom

        # Prior to ZODB 4.3.1, ConflictResolvingStorage would raise an AttributeError
        super().registerDB(wrapper)

    def isReadOnly(self):
        return self._is_read_only

    def checkCurrentSerialInTransaction(self, oid, serial, transaction):
        self._tpc_phase.checkCurrentSerialInTransaction(oid, serial, transaction)

    # For the time between tpc_begin and tpc_abort, if anything we do
    # would trigger an exception to propagate up (store(), tpc_vote(),
    # whatever) we should immediately abort the transaction at the
    # database level and release locks. This will allow other
    # transactions to make progress faster. It also can help in the ZODB test suite,
    # since some places manually call the tpc_* methods and aren't careful to
    # tpc_abort() in the event of an exception like `transaction` is.
    #
    # Some of this is manual (in the tpc_* methods), but most of it is
    # done through the @phase_dependent_aborts_early decorator.

    @metricmethod
    def tpc_begin(self, transaction, tid=None, status=' '):
        try:
            self._tpc_phase = self._tpc_phase.tpc_begin(self, transaction)
        except:
            # Could be a database (connection) error, could be a programming
            # bug. Either way, we're fine to roll everything back and hope
            # for the best on a retry. Perhaps we need to raise a TransientError?
            self._load_connection.drop()
            raise

        if tid is not None:
            # tid is a committed transaction we will restore.
            # The allowed actions are carefully prescribed.
            # This argument is specified by IStorageRestoreable
            try:
                next_phase = Restore(self._tpc_phase, tid, status)
            except:
                self.tpc_abort(transaction, _force=True)
                raise

            self._tpc_phase = next_phase

    @metricmethod
    def tpc_vote(self, transaction):
        # Returns an iterable of OIDs; the storage will ghost all
        # cached objects in that list. This is invalidation because
        # the object has changed during the commit process, due to
        # conflict resolution or undo.
        try:
            next_phase = self._tpc_phase.tpc_vote(self, transaction)
        except TransientError:
            # Most likely a ConflictError of some sort, including
            # UnableToLockRows.... Not necessary to force close
            # connections, it's not database related.
            self.tpc_abort(transaction)
            raise
        except:
            # force will cause connections to get closed, which is slow.
            # Only do that for really unexpected exceptions.
            self.tpc_abort(transaction, _force=True)
            raise

        self._tpc_phase = next_phase
        return next_phase.invalidated_oids

    @metricmethod
    def tpc_finish(self, transaction, f=None):
        try:
            next_phase = self._tpc_phase.tpc_finish(self, transaction, f)
        except:
            # OH NO! This isn't supposed to happen!
            # It's unlikely tpc_abort will get called...
            self.tpc_abort(transaction, _force=True)
            raise
        # The store connection is either committed or rolledback;
        # the load connection is now rolledback.
        self._tpc_phase = next_phase
        # It might be nice to de-dup this so we're not storing it both
        # in the phase and in self, but if it was needed during TPC,
        # when our phase is not ``ITPCStateNotInTransaction``, we couldn't
        # get it.
        committed_tid = int64_to_8bytes(next_phase.last_committed_tid_int)
        self._last_tid_i_committed_bytes = committed_tid
        return committed_tid

    @metricmethod
    def tpc_abort(self, transaction, _force=False):
        # _force is not a public argument, it is an internal
        # implementation detail.
        try:
            self._tpc_phase = self._tpc_phase.tpc_abort(transaction, _force)
        except BaseException:
            if _force:
                # We're here under unexpected circumstances. It's possible something
                # might go wrong rolling back.
                self._tpc_phase = self._tpc_phase.initial_state
            raise

    def lastTransaction(self):
        if self._last_tid_i_committed_bytes == z64 and self._cache.highest_visible_tid is None:
            # We haven't committed *or* polled for transactions,
            # so our MVCC state is "floating".
            # Read directly from the database to get the latest value,
            return int64_to_8bytes(self._adapter.txncontrol.get_tid(self._load_connection.cursor))

        return max(self._last_tid_i_committed_bytes,
                   int64_to_8bytes(self._cache.highest_visible_tid or 0))

    def lastTransactionInt(self):
        return bytes8_to_int64(self.lastTransaction())

    def new_oid(self):
        # This is called from ``Connection.add`` which can be called at any time
        # by the application, so we don't know what state we're in. It is also called from
        # ``Connection._commit``, which is called during TPC.
        # If we're committing, we can't restart the connection.
        pool = self._store_connection_pool
        commit_in_progress = False
        if self._tpc_phase:
            commit_in_progress = True
            pool = SingleConnectionPool(self._tpc_phase.shared_state.store_connection)

        return self._oids.new_oid(pool, commit_in_progress)

    def iterator(self, start=None, stop=None):
        # XXX: This is broken for purposes of copyTransactionsFrom() because
        # it can only be iterated over once. zodbconvert works around this.
        if self.keep_history:
            return HistoryPreservingTransactionIterator(self._adapter, start, stop)
        return HistoryFreeTransactionIterator(
            self._adapter, self._load_connection, start, stop)

    __next = next

    def __record_iternext_gen(self, start_oid_int):
        # XXX: This needs to support __len__()
        # XXX: If this produces Blob records, the receiver will call
        # ``openCommittedBlobFile()`` on us. That also uses the load connection
        # (it has to, in order to be consistent). The problem is that one driver,
        # PyMySQLConnector, can't handle a second query when this cursor is still active.
        # It raises ``.InternalError: Unread result found``. This manifests in
        # ``testSimpleBlobRecovery``. But, for some reason, that only happens when executing
        # ``__on_load_first_use``. If we already had the cursor *open*, then it can be used fine,
        # at least so it seems. So here we make sure to have the cursor open.
        getattr(self._load_connection, 'cursor')
        with self._load_connection.server_side_cursor() as ss_cursor:
            yield from self._adapter.dbiter.iter_current_records(ss_cursor, start_oid_int)

    def record_iternext(self, next=None): # pylint:disable=redefined-builtin
        """
        Implementation of `ZODB.interfaces.IStorageCurrentRecordIteration`.

        .. caution::

           You must completely consume the iteration.
        """
        # The interface doesn't define any semantics for *next*; it's
        # completely up to the storage and is just a magic cookie.
        # However, zodbupdate assumes that the initial value for
        # `next` is interpreted like FileStorage does: as an OID (8
        # bytes) and uses that to specify an object id to start
        # with. (inclusive)
        #
        # We need to detect the case that we're given an OID; that should only happen
        # in the first call.
        start_oid_int = 0
        if isinstance(next, bytes):
            import warnings
            warnings.warn(
                "There is no defined value for the *next* parameter. "
                "RelStorage will rely on implementation-defined behaviour and treat it like "
                "FileStorage does, while assuming that iteration is beginning from the start. "
                "This may change in the future."
            )
            start_oid_int = bytes8_to_int64(next)
            next = None

        if next is None:
            # Beginning.
            cursor = self.__record_iternext_gen(start_oid_int)
            # There *should* always be at least one object: the root.
            # But if they passed a non-0 ``next`` value, that's not guaranteed.
            # However, the protocol is not very good at expressing that possibility:
            # The typical call looks like:
            #    oid, tid, data, next = storage.record_iternext(next)
            # Unpacking will raise if we return None; most processing of *data* is not expecting
            # None (it's common to wrap it in ``io.BytesIO()`` immediately; that's what zodbupdate
            # does). Thus, it's probably probably best to let getting the first value simply raise.
            # In that scenario, FileStorage raises a ValueError:
            # https://github.com/zopefoundation/ZODB/issues/330
            oid, tid, state = self.__next(cursor)
            # After that, we can treat it like it was given to us in *next*
            next = oid, tid, state, cursor


        # Somewhere in the middle, possibly the end.
        #
        # We have to operate one ahead (keep one buffered) because
        # we need to be able to tell the caller not to call back.
        oid, tid, state, cursor = next

        try:
            new_oid, new_tid, new_state = self.__next(cursor)
        except StopIteration:
            # Signal not to call back.
            new_next = None
        else:
            new_next = new_oid, new_tid, new_state, cursor

        return oid, tid, state, new_next


    def afterCompletion(self):
        # Note that this method exists mainly to deal with read-only
        # transactions that don't go through 2-phase commit (although
        # it's called for all transactions). For this reason, we only
        # have to roll back the load connection. (The store connection
        # is completed during normal write-transaction commit or
        # abort.)

        # The next time we use the load connection, it will need to poll
        # and will call our __on_first_use.
        # Typically our next call from the ZODB Connection will be from its
        # `newTransaction` method, a forced `sync` followed by `poll_invalidations`.

        # As with `poll_invalidations`, the cache needs to know about transaction
        # boundaries like this to optimize polling for changes and invalidations.
        # If we were a write transaction, that's already done by the call to ``after_tpc_finish``,
        # but for a read-only transaction, it wouldn't happen. So for this reason,
        # as with `poll_invalidations`, the cache is in charge.

        # This doesn't use restart_and_call() or even just restart() because we don't
        # need to do those checks yet, we just want to quietly rollback.
        # They both rollback; the difference is that restart_load checks for replicas,
        # and calls any hooks needed.
        self._load_connection.rollback_quietly()

    def sync(self, force=True):
        """
        Updates to a current view of the database.

        This is implemented by rolling back the relational database
        transaction.

        .. versionchanged:: 3.0a6
           This method now pays attention to the *force* argument. If it is
           ``False``, this method does nothing. This prevents extra rollbacks
           and network traffic in the common case of using the default implicit
           transactions when this object is used by a ZODB ``Connection``.

        """
        if not force:
            # When we get a force=False, it's from Connection.afterCompletion()
            # calling Connection.newTransaction() because the transaction manager is
            # in implicit mode. Immediately after it does that, it will call
            # poll_invalidations()...and in our implementation, that implies a sync.
            # So we can avoid the overhead of the extra rollback.
            return

        rolled_back = self._load_connection.rollback_quietly()
        if not rolled_back:
            # Disconnected. Well, the rollback happens automatically
            # in that case. No big deal, ignore it.
            #
            # However, if we happened to be in the middle of committing and were
            # asked to sync (XXX: why would we?) then that's probably a problem that
            # needs to be handled and the commit rolled back too.
            if self._tpc_phase:
                raise StorageDisconnectedDuringCommit()

    def poll_invalidations(self):
        """
        Look for OIDs of objects that changed since _prev_polled_tid.

        Returns {oid: 1}, or None if all objects need to be invalidated
        because prev_polled_tid is not in the database (presumably it
        has been packed).
        """
        # poll_invalidations is called by Connection.newTransaction(), which is a
        # transaction synchronizer method.
        # Connection.afterCompletion() also calls Connection.newTransaction() if we're not
        # using explicit transactions.
        # Our sync() will have been called just before this, so we should not need to
        # restart the connection, just make a call. The load_connection logic handles that
        # for us.

        if self._closed:
            # If we've never loaded (self._load_connection is false),
            # it might seem that there's no point in doing a poll, but
            # that's not true. testMVCC breaks if we skip this. It's not exactly clear
            # why.
            return {}

        # __on_load_first_use is also the function we gave the LoadConnection to automatically
        # call when we access the cursor for the first time. In this situation, we optimize
        # for when the function is the same and only call it once.
        self._load_connection.restart_and_call(
            self.__on_load_first_use
        )
        # Now we're in a fully synced state, meaning
        # we don't need to do anything fancy with the load connection
        # anymore. Let it know that.
        self._load_connection.active = True

        changed_oids = self.__queued_changes
        self.__queued_changes = OID_SET_TYPE()

        if changed_oids is None:
            oids = None
        else:
            # The Connection doesn't care about this, it just passes it
            # to the PickleCache. The Python implementation of the PickleCache
            # takes any iterable of oid bytes, but the C implementation deals only
            # with an actual dict(), or something that is a sequence and can be
            # iterated using integer indices. If you give it a dict, all it cares
            # about are the keys.
            oids = {int64_to_8bytes(oid_int): 1 for oid_int in changed_oids}
        return oids

    def __stale(self, stale_error):
        # Reset the Connection cache. This makes it more likely we'll be called to
        # load an object and the application can notice the issue.
        self.__queued_changes = None
        # Allow GC to do its thing with the locals, don't hold them indefinitely
        clear_frames(sys.exc_info()[2])

        replacements = {}
        my_ns = vars(self)
        for k, v in my_ns.items():
            if callable(getattr(v, 'stale', None)):
                new_v = v.stale(stale_error)
                replacements[k] = new_v

        my_ns.update(replacements)

        # The next time we rollback or reopen, we're no longer stale
        # Note that this creates a reference cycle, but it should only be
        # temporary.
        self._load_connection.on_rolledback = self.__no_longer_stale
        self._load_connection.on_opened = self.__no_longer_stale

    def __no_longer_stale(self, _conn, _cursor):
        # This is called at the end of every transaction in
        # afterCompletion(), and at the beginning of every transaction
        # in sync() (called from Connection.newTransaction(), which is
        # called automatically from Connection.afterCompletion when
        # transactions are not explicit) Most of the time we won't be
        # stale, which is why we only install these hooks when we are,
        # and are careful to clean them up when we're not.
        #
        # Both _conn and _cursor will be None if the rollback detected
        # a disconnect from the database.

        my_ns = vars(self)
        replacements = {
            k: v.no_longer_stale()
            for k, v in my_ns.items()
            if callable(getattr(v, 'no_longer_stale', None))
        }

        my_ns.update(replacements)

        del self._load_connection.on_rolledback
        del self._load_connection.on_opened

    def __on_load_first_use(self, conn, cursor):
        """
        Poll for invalidations, update our cache.

        Move the component objects of this object into or out of the
        stale state as appropriate.

        Primarily in tests which directly use storage APIs, this may
        be called automatically, between transactions, when a
        Connection is not in use to receive invalidations. For that
        reason, we queue changes seen here as we keep moving forward
        and can apply them all when a connection asks us.
        """

        # Ignore changes made by the last transaction committed
        # by this connection, we don't want to ghost objects that we're sure
        # are up-to-date unless someone else has changed them.
        # Note that transactions can happen between us committing and polling.
        if self._last_tid_i_committed_bytes is not None:
            ignore_tid = bytes8_to_int64(self._last_tid_i_committed_bytes)
        else:
            ignore_tid = None

        # get a list of changed OIDs and the most recent tid
        try:
            changes = self._cache.poll(
                conn, cursor,
                ignore_tid
            )
        except (ReadConflictError, CacheConsistencyError) as e:
            # The database connection or cache is stale, but postpone
            # this error until the application tries to read or write
            # something and is better equipped to deal with it. (This
            # is called from transaction.begin() ->
            # Connection.newTransaction ->
            # RelStorage.poll_invalidations, and no one is prepared
            # for transaction.begin() to fail, or to handle it
            # gracefully if it does.)

            # If we were configured to revert-when-stale, we wouldn't
            # get a ReadConflictError, we'd have a return of None and
            # our cache would restart polling. Eventually the RCE
            # should stop as the replica catches up. But if we clear
            # the caches on the RCE, they'll lose their polling state
            # and essentially revert-when-stale even though we weren't
            # configured to do so.

            # At a minimum, though, we must reset our Connection's
            # object cache. If we fail do do this, then we see issue
            # in the zodbconvert tests, when the destination storage
            # configuration loaded to do the conversion and zapping on
            # didn't have matching persistent cache information as the
            # storage configuration that had initially populated the
            # destination (in order to verify zapping). As our cache
            # implementation got better and better, we suddenly found
            # that without zapping the cache files, we would report
            # this RCE, based on info from the persistent cache from
            # before the conversion, with no way to fix it (short of
            # zapping those cache files). This can also come up with
            # packing in history-free databases
            # (checkPackAllRevisions). We have a temporary fix for
            # that in self.pack() until we write more specific tests.
            #
            # TODO: We need a way to distinguish RCE from packing
            # vs RCE from a replica switch.

            # A CCE, on the other hand, means the cache has already
            # reset its own internal state, we outght to do the same for
            # the Connection. See above for why this shouldn't
            # propagate.
            log.error("Reading from stale replica or leading transactions packed away; %s", e)
            self.__stale(e)
            changes = None


        if changes is None:
            # Stop tracking invalidations and reset the ZODB object cache.
            # This is reset by poll_invalidations.
            self.__queued_changes = None
        elif self.__queued_changes is not None:
            self.__queued_changes.update(changes)
            if len(self.__queued_changes) > self._options.cache_delta_size_limit:
                # Hmm, ok, the Connection isn't polling us in a timely fashion.
                # Maybe we're the root storage? Maybe our APIs are being used
                # independently? At any rate, we're going to stop tracking now;
                # if a Connection eventually gets around to polling us, they'll
                # need to clear their whole cache
                self.__queued_changes = None

    def temporaryDirectory(self):
        """Return a directory that should be used for uncommitted blob data.

        If Blobs use this, then commits can be performed with a simple rename.
        """
        return self.blobhelper.temporaryDirectory()

    def copyTransactionsFrom(self, other):
        Copy(self.blobhelper, self, self).copyTransactionsFrom(other)
        self._adapter.stats.large_database_change()

    def pack(self, t, referencesf, prepack_only=False, skip_prepack=False, check_refs=False):
        # Force pack_gc to on while checking references; otherwise we don't traverse the
        # tree and nothing happens.
        options = self._options.copy(pack_gc=True) if check_refs else self._options
        pack = Pack(options, self._adapter, self.blobhelper, self._cache)
        if check_refs:
            assert pack.options.pack_gc
            result = pack.check_refs(referencesf)
        else:
            result = pack.pack(t, referencesf, prepack_only, skip_prepack)
            if not self.keep_history:
                # In a history free database, it's *possible*
                # that the database's last transaction ID could now have actually
                # gone backwards! I strongly suspect this only happens in fabricated
                # test scenarios (e.g., the last transaction in the database is an object that
                # was directly stored without any connection to the object graph starting from the
                # root and hence was packed away). Not only do we need to restart our
                # connection, we also need to restart our polling. Moreover, to prevent
                # loadSerial() from finding cached data, we also need to flush our caches.
                self._cache.clear(load_persistent=False)

            self.sync()

            self._pack_finished()
        return result

    def _pack_finished(self):
        "Hook for testing."


try:
    from zc import zlibstorage
except ImportError: # pragma: no cover
    zlibstorage = None

def _zlibstorage_new_instance(self):
    # We have to work hard to preserve the `_transform`
    # value. This is set based on the `compress` argument given to the
    # constructor, but only captured in the value of the _transform
    # attribute. This must be given to the constructor
    # directly because it calls registerDB, which goes up the MRO and sets
    # some attributes like _crs_transform_record_data, etc. (The argument is passed
    # when opening the storage from ZConfig.)
    #
    # It's not a smart idea copying the dict directly, it contains mutable things like
    # __provides__ that we don't want to share.
    new_base = self.base.new_instance()
    # pylint:disable-next=protected-access
    new_self = type(self)(new_base, compress=self._transform is zlibstorage.compress)

    return new_self

def _zlibstorage_pack(self, pack_time, referencesf, *args, **kwargs):
    # pylint:disable-next=protected-access
    untransform = self._untransform
    def refs(state, oids=None):
        return referencesf(untransform(state), oids)
    return self.base.pack(pack_time, refs, *args, **kwargs)

def _zlibstorage_Iterator_len(self):
    # pylint:disable-next=protected-access
    return len(self._base_it)
