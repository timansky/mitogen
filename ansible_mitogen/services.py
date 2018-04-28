# Copyright 2017, David Wilson
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""
Classes in this file define Mitogen 'services' that run (initially) within the
connection multiplexer process that is forked off the top-level controller
process.

Once a worker process connects to a multiplexer process
(Connection._connect()), it communicates with these services to establish new
connections, grant access to files by children, and register for notification
when a child has completed a job.
"""

from __future__ import absolute_import
import grp
import logging
import os
import os.path
import pwd
import stat
import sys
import threading
import zlib

import mitogen
import mitogen.service
import ansible_mitogen.target


LOG = logging.getLogger(__name__)


class Error(Exception):
    pass


class ContextService(mitogen.service.Service):
    """
    Used by workers to fetch the single Context instance corresponding to a
    connection configuration, creating the matching connection if it does not
    exist.

    For connection methods and their parameters, see:
        https://mitogen.readthedocs.io/en/latest/api.html#context-factories

    This concentrates connections in the top-level process, which may become a
    bottleneck. The bottleneck can be removed using per-CPU connection
    processes and arranging for the worker to select one according to a hash of
    the connection parameters (sharding).
    """
    handle = 500
    max_message_size = 1000
    max_interpreters = int(os.getenv('MITOGEN_MAX_INTERPRETERS', '20'))

    def __init__(self, *args, **kwargs):
        super(ContextService, self).__init__(*args, **kwargs)
        self._lock = threading.Lock()
        #: Records the :meth:`get` result dict for successful calls, returned
        #: for identical subsequent calls. Keyed by :meth:`key_from_kwargs`.
        self._response_by_key = {}
        #: List of :class:`mitogen.core.Latch` awaiting the result for a
        #: particular key.
        self._latches_by_key = {}
        #: Mapping of :class:`mitogen.core.Context` -> reference count. Each
        #: call to :meth:`get` increases this by one. Calls to :meth:`put`
        #: decrease it by one.
        self._refs_by_context = {}
        #: List of contexts in creation order by via= parameter. When
        #: :attr:`max_interpreters` is reached, the most recently used context
        #: is destroyed to make room for any additional context.
        self._lru_by_via = {}
        #: :meth:`key_from_kwargs` result by Context.
        self._key_by_context = {}

    @mitogen.service.expose(mitogen.service.AllowParents())
    @mitogen.service.arg_spec({
        'context': mitogen.core.Context
    })
    def put(self, context):
        """
        Return a reference, making it eligable for recycling once its reference
        count reaches zero.
        """
        LOG.debug('%r.put(%r)', self, context)
        if self._refs_by_context.get(context, 0) == 0:
            LOG.warning('%r.put(%r): refcount was 0. shutdown_all called?',
                        self, context)
            return
        self._refs_by_context[context] -= 1

    def key_from_kwargs(self, **kwargs):
        """
        Generate a deduplication key from the request.
        """
        out = []
        stack = [kwargs]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                stack.extend(sorted(obj.iteritems()))
            elif isinstance(obj, (list, tuple)):
                stack.extend(obj)
            else:
                out.append(str(obj))
        return ''.join(out)

    def _produce_response(self, key, response):
        """
        Reply to every waiting request matching a configuration key with a
        response dictionary, deleting the list of waiters when done.

        :param str key:
            Result of :meth:`key_from_kwargs`
        :param dict response:
            Response dictionary
        :returns:
            Number of waiters that were replied to.
        """
        self._lock.acquire()
        try:
            latches = self._latches_by_key.pop(key)
            count = len(latches)
            for latch in latches:
                latch.put(response)
        finally:
            self._lock.release()
        return count

    def _shutdown(self, context, lru=None, new_context=None):
        """
        Arrange for `context` to be shut down, and optionally add `new_context`
        to the LRU list while holding the lock.
        """
        LOG.info('%r._shutdown(): shutting down %r', self, context)
        context.shutdown()

        key = self._key_by_context[context]

        self._lock.acquire()
        try:
            del self._response_by_key[key]
            del self._refs_by_context[context]
            del self._key_by_context[context]
            if lru:
                lru.remove(context)
            if new_context:
                lru.append(new_context)
        finally:
            self._lock.release()

    def _update_lru(self, new_context, spec, via):
        """
        Update the LRU ("MRU"?) list associated with the connection described
        by `kwargs`, destroying the most recently created context if the list
        is full. Finally add `new_context` to the list.
        """
        lru = self._lru_by_via.setdefault(via, [])
        if len(lru) < self.max_interpreters:
            lru.append(new_context)
            return

        for context in reversed(lru):
            if self._refs_by_context[context] == 0:
                break
        else:
            LOG.warning('via=%r reached maximum number of interpreters, '
                        'but they are all marked as in-use.', via)
            return

        self._shutdown(context, lru=lru, new_context=new_context)

    @mitogen.service.expose(mitogen.service.AllowParents())
    def shutdown_all(self):
        """
        For testing use, arrange for all connections to be shut down.
        """
        for context in list(self._key_by_context):
            self._shutdown(context)
        self._lru_by_via = {}

    def _on_stream_disconnect(self, stream):
        """
        Respond to Stream disconnection by deleting any record of contexts
        reached via that stream. This method runs in the Broker thread and must
        not to block.
        """
        # TODO: there is a race between creation of a context and disconnection
        # of its related stream. An error reply should be sent to any message
        # in _latches_by_key below.
        self._lock.acquire()
        try:
            for context, key in list(self._key_by_context.items()):
                if context.context_id in stream.routes:
                    LOG.info('Dropping %r due to disconnect of %r',
                             context, stream)
                    self._response_by_key.pop(key, None)
                    self._latches_by_key.pop(key, None)
                    self._refs_by_context.pop(context, None)
                    self._lru_by_via.pop(context, None)
                    self._refs_by_context.pop(context, None)
        finally:
            self._lock.release()

    def _connect(self, key, spec, via=None):
        """
        Actual connect implementation. Arranges for the Mitogen connection to
        be created and enqueues an asynchronous call to start the forked task
        parent in the remote context.

        :param key:
            Deduplication key representing the connection configuration.
        :param spec:
            Connection specification.
        :returns:
            Dict like::

                {
                    'context': mitogen.core.Context or None,
                    'home_dir': str or None,
                    'msg': str or None
                }

            Where either `msg` is an error message and the remaining fields are
            :data:`None`, or `msg` is :data:`None` and the remaining fields are
            set.
        """
        try:
            method = getattr(self.router, spec['method'])
        except AttributeError:
            raise Error('unsupported method: %(transport)s' % spec)

        context = method(via=via, **spec['kwargs'])
        if via:
            self._update_lru(context, spec, via)
        else:
            # For directly connected contexts, listen to the associated
            # Stream's disconnect event and use it to invalidate dependent
            # Contexts.
            stream = self.router.stream_by_id(context.context_id)
            mitogen.core.listen(stream, 'disconnect',
                                lambda: self._on_stream_disconnect(stream))

        home_dir = context.call(os.path.expanduser, '~')

        # We don't need to wait for the result of this. Ideally we'd check its
        # return value somewhere, but logs will catch a failure anyway.
        context.call_async(ansible_mitogen.target.start_fork_parent)

        if os.environ.get('MITOGEN_DUMP_THREAD_STACKS'):
            from mitogen import debug
            context.call(debug.dump_to_logger)

        self._key_by_context[context] = key
        self._refs_by_context[context] = 0
        return {
            'context': context,
            'home_dir': home_dir,
            'msg': None,
        }

    def _wait_or_start(self, spec, via=None):
        latch = mitogen.core.Latch()
        key = self.key_from_kwargs(via=via, **spec)
        self._lock.acquire()
        try:
            response = self._response_by_key.get(key)
            if response is not None:
                self._refs_by_context[response['context']] += 1
                latch.put(response)
                return latch

            latches = self._latches_by_key.setdefault(key, [])
            first = len(latches) == 0
            latches.append(latch)
        finally:
            self._lock.release()

        if first:
            # I'm the first requestee, so I will create the connection.
            try:
                response = self._connect(key, spec, via=via)
                count = self._produce_response(key, response)
                # Only record the response for non-error results.
                self._response_by_key[key] = response
                # Set the reference count to the number of waiters.
                self._refs_by_context[response['context']] += count
            except Exception:
                self._produce_response(key, sys.exc_info())

        return latch

    @mitogen.service.expose(mitogen.service.AllowParents())
    @mitogen.service.arg_spec({
        'stack': list
    })
    def get(self, msg, stack):
        """
        Return a Context referring to an established connection with the given
        configuration, establishing new connections as necessary.

        :param list stack:
            Connection descriptions. Each element is a dict containing 'method'
            and 'kwargs' keys describing the Router method and arguments.
            Subsequent elements are proxied via the previous.

        :returns dict:
            * context: mitogen.master.Context or None.
            * homedir: Context's home directory or None.
            * msg: StreamError exception text or None.
            * method_name: string failing method name.
        """
        via = None
        for spec in stack:
            try:
                result = self._wait_or_start(spec, via=via).get()
                if isinstance(result, tuple):  # exc_info()
                    e1, e2, e3 = result
                    raise e1, e2, e3
                via = result['context']
            except mitogen.core.StreamError as e:
                return {
                    'context': None,
                    'home_dir': None,
                    'method_name': spec['method'],
                    'msg': str(e),
                }

        return result


class FileService(mitogen.service.Service):
    """
    Streaming file server, used to serve both small files like Ansible module
    sources, and huge files like ISO images. Paths must be explicitly added to
    the service by a trusted context before they will be served to an untrusted
    context.

    The file service nominally lives on the mitogen.service.Pool() threads
    shared with ContextService above, however for simplicity it also maintains
    a dedicated thread from where file chunks are scheduled.

    The scheduler thread is responsible for dividing transfer requests up among
    the physical streams that connect to those contexts, and ensure each stream
    never has an excessive amount of data buffered in RAM at any time.

    Transfers proceeed one-at-a-time per stream. When multiple contexts exist
    reachable over the same stream (e.g. one is the SSH account, another is a
    sudo account, and a third is a proxied SSH connection), each request is
    satisfied in turn before chunks for subsequent requests start flowing. This
    ensures when a connection is contended, that preference is given to
    completing individual transfers, rather than potentially aborting many
    partially complete transfers, causing all the bandwidth used to be wasted.

    Theory of operation:
        1. Trusted context (i.e. a WorkerProcess) calls register(), making a
           file available to any untrusted context.
        2. Untrusted context creates a mitogen.core.Receiver() to receive
           file chunks. It then calls fetch(path, recv.to_sender()), which sets
           up the transfer. The fetch() method returns the final file size and
           notifies the dedicated thread of the transfer request.
        3. The dedicated thread wakes from perpetual sleep, looks up the stream
           used to communicate with the untrusted context, and begins pumping
           128KiB-sized chunks until that stream's output queue reaches a
           limit (1MiB).
        4. The thread sleeps for 10ms, wakes, and pumps new chunks as necessary
           to refill any drained output queue, which are being asynchronously
           drained by the Stream implementation running on the Broker thread.
        5. Once the last chunk has been pumped for a single transfer,
           Sender.close() is called causing the receive loop in
           target.py::_get_file() to exit, and allows that code to compare the
           transferred size with the total file size indicated by the return
           value of the fetch() method.
        6. If the sizes mismatch, the caller is informed, which will discard
           the result and log an error.
        7. Once all chunks have been pumped for all transfers, the dedicated
           thread stops waking at 10ms intervals and resumes perpetual sleep.

    Shutdown:
        1. process.py calls service.Pool.shutdown(), which arranges for all the
           service pool threads to exit and be joined, guranteeing no new
           requests can arrive, before calling Service.on_shutdown() for each
           registered service.
        2. FileService.on_shutdown() marks the dedicated thread's queue as
           closed, causing the dedicated thread to wake immediately. It will
           throw an exception that begins shutdown of the main loop.
        3. The main loop calls Sender.close() prematurely for every pending
           transfer, causing any Receiver loops in the target contexts to exit
           early. The file size check fails, and the partially downloaded file
           is discarded, and an error is logged.
        4. Control exits the file transfer function in every target, and
           graceful target shutdown can proceed normally, without the
           associated thread needing to be forcefully killed.
    """
    handle = 501
    max_message_size = 1000
    unregistered_msg = 'Path is not registered with FileService.'

    #: Maximum size of any stream's output queue before we temporarily stop
    #: pumping more file chunks on that stream. The queue may overspill by up
    #: to mitogen.core.CHUNK_SIZE-1 bytes (128KiB-1).
    max_queue_size = 1048576

    #: Time spent by the scheduler thread asleep when it has no more data to
    #: pump, but while at least one transfer remains active. With
    #: max_queue_size=1MiB and a sleep of 10ms, maximum throughput on any
    #: single stream is 112MiB/sec, which is >5x what SSH can handle on my
    #: laptop.
    sleep_delay_secs = 0.01

    def __init__(self, router):
        super(FileService, self).__init__(router)
        #: Mapping of registered path -> file size.
        self._metadata_by_path = {}
        #: Queue used to communicate from service to scheduler thread.
        self._queue = mitogen.core.Latch()
        #: Mapping of Stream->[(Sender, file object)].
        self._pending_by_stream = {}
        self._thread = threading.Thread(target=self._scheduler_main)
        self._thread.start()

    def on_shutdown(self):
        """
        Respond to shutdown of the service pool by marking our queue closed.
        This causes :meth:`_sleep_on_queue` to wake immediately and return
        :data:`False`, causing the scheduler main thread to exit.
        """
        self._queue.close()

    def _pending_bytes(self, stream):
        """
        Defer a function call to the Broker thread in order to accurately
        measure the bytes pending in `stream`'s queue.

        This must be done synchronized with the Broker, as OS scheduler
        uncertainty could cause Sender.send()'s deferred enqueues to be
        processed very late, making the output queue look much emptier than it
        really is (or is about to become).
        """
        latch = mitogen.core.Latch()
        self.router.broker.defer(lambda: latch.put(stream.pending_bytes()))
        return latch.get()

    def _schedule_pending(self, stream, pending):
        """
        Consider the pending file transfers for a single stream, pumping new
        file chunks into the stream's queue while its size is below the
        configured limit.

        :param mitogen.core.Stream stream:
            Stream to pump chunks for.
        :param pending:
            Corresponding list from :attr:`_pending_by_stream`.
        """
        while pending and self._pending_bytes(stream) < self.max_queue_size:
            sender, fp = pending[0]
            s = fp.read(mitogen.core.CHUNK_SIZE)
            if s:
                sender.send(s)
                continue

            # Empty read, indicating this file is fully transferred. Mark the
            # sender closed (causing the corresponding Receiver loop in the
            # target to exit), close the file handle, remove our entry from the
            # pending list, and delete the stream's entry in the pending map if
            # no more sends remain.
            sender.close()
            fp.close()
            pending.pop(0)
            if not pending:
                del self._pending_by_stream[stream]

    def _sleep_on_queue(self):
        """
        Sleep indefinitely (no active transfers) or for
        :attr:`sleep_delay_secs` (active transfers) waiting for a new transfer
        request to arrive from the :meth:`fetch` method.

        If a new request arrives, add it to the appropriate list in
        :attr:`_pending_by_stream`.

        :returns:
            :data:`True` the scheduler's queue is still open,
            :meth:`on_shutdown` hasn't been called yet, otherwise
            :data:`False`.
        """
        if self._pending_by_stream:
            timeout = self.sleep_delay_secs
        else:
            timeout = None

        try:
            sender, fp = self._queue.get(timeout=timeout)
        except mitogen.core.LatchError:
            return False
        except mitogen.core.TimeoutError:
            return True

        LOG.debug('%r._sleep_on_queue(): setting up %r for %r',
                  self, fp.name, sender)
        stream = self.router.stream_by_id(sender.context.context_id)
        pending = self._pending_by_stream.setdefault(stream, [])
        pending.append((sender, fp))
        return True

    def _scheduler_main(self):
        """
        Scheduler thread's main function. Sleep until
        :meth:`_sleep_on_queue` indicates the queue has been shut down,
        pumping pending file chunks each time we wake.
        """
        while self._sleep_on_queue():
            for stream, pending in list(self._pending_by_stream.items()):
                self._schedule_pending(stream, pending)

        # on_shutdown() has been called. Send close() on every sender to give
        # targets a chance to shut down gracefully.
        LOG.debug('%r._scheduler_main() shutting down', self)
        for _, pending in self._pending_by_stream.items():
            for sender, fp in pending:
                sender.close()
                fp.close()

    def _name_or_none(self, func, n, attr):
        try:
            return getattr(func(n), attr)
        except KeyError:
            return None

    @mitogen.service.expose(policy=mitogen.service.AllowParents())
    @mitogen.service.arg_spec({
        'path': basestring
    })
    def register(self, path):
        """
        Authorize a path for access by child contexts. Calling this repeatedly
        with the same path is harmless.

        :param str path:
            File path.
        """
        if path in self._metadata_by_path:
            return

        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            raise IOError('%r is not a regular file.' % (in_path,))

        LOG.debug('%r: registering %r', self, path)
        self._metadata_by_path[path] = {
            'size': st.st_size,
            'mode': st.st_mode,
            'owner': self._name_or_none(pwd.getpwuid, 0, 'pw_name'),
            'group': self._name_or_none(grp.getgrgid, 0, 'gr_name'),
            'mtime': st.st_mtime,
            'atime': st.st_atime,
        }

    @mitogen.service.expose(policy=mitogen.service.AllowAny())
    @mitogen.service.arg_spec({
        'path': basestring,
        'sender': mitogen.core.Sender,
    })
    def fetch(self, path, sender):
        """
        Fetch a file's data.

        :param str path:
            File path.
        :param mitogen.core.Sender sender:
            Sender to receive file data.
        :returns:
            Dict containing the file metadata:

            * ``size``: File size in bytes.
            * ``mode``: Integer file mode.
            * ``owner``: Owner account name on host machine.
            * ``group``: Owner group name on host machine.
            * ``mtime``: Floating point modification time.
            * ``ctime``: Floating point change time.
        :raises mitogen.core.CallError:
            The path was not registered.
        """
        if path not in self._metadata_by_path:
            raise mitogen.core.CallError(self.unregistered_msg)

        LOG.debug('Serving %r', path)
        fp = open(path, 'rb', mitogen.core.CHUNK_SIZE)
        self._queue.put((sender, fp))
        return self._metadata_by_path[path]
