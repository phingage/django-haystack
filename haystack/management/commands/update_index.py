# encoding: utf-8
from __future__ import absolute_import, division, print_function, unicode_literals

import logging
import os
import sys
import warnings
from datetime import timedelta
from optparse import make_option

try:
    from django.db import close_old_connections
except ImportError:
    from django.db import close_connection as close_old_connections

    warnings.warn('close_connection has been removed from Django 1.8+',
                  DeprecationWarning)

from django.core.management.base import LabelCommand
from django.db import reset_queries

from haystack import connections as haystack_connections
from haystack.query import SearchQuerySet
from haystack.utils.app_loading import haystack_get_models, haystack_load_apps

try:
    from django.utils.encoding import force_text
except ImportError:
    from django.utils.encoding import force_unicode as force_text

try:
    from django.utils.encoding import smart_bytes
except ImportError:
    from django.utils.encoding import smart_str as smart_bytes

try:
    from django.utils.timezone import now
except ImportError:
    from datetime import datetime
    now = datetime.now


DEFAULT_BATCH_SIZE = None
DEFAULT_AGE = None
APP = 'app'
MODEL = 'model'


def worker(bits):
    # We need to reset the connections, otherwise the different processes
    # will try to share the connection, which causes things to blow up.
    from django.db import connections

    for alias, info in connections.databases.items():
        # We need to also tread lightly with SQLite, because blindly wiping
        # out connections (via ``... = {}``) destroys in-memory DBs.
        if 'sqlite3' not in info['ENGINE']:
            try:
                close_old_connections()
                if isinstance(connections._connections, dict):
                    del(connections._connections[alias])
                else:
                    delattr(connections._connections, alias)
            except KeyError:
                pass

    if bits[0] == 'do_update':
        func, model, start, end, total, using, start_date, end_date, verbosity, commit = bits
    elif bits[0] == 'do_remove':
        func, model, pks_seen, start, upper_bound, using, verbosity, commit = bits
    else:
        return

    unified_index = haystack_connections[using].get_unified_index()
    index = unified_index.get_index(model)
    backend = haystack_connections[using].get_backend()

    if func == 'do_update':
        qs = index.build_queryset(start_date=start_date, end_date=end_date)
        do_update(backend, index, qs, start, end, total, verbosity=verbosity, commit=commit)
    elif bits[0] == 'do_remove':
        do_remove(backend, index, model, pks_seen, start, upper_bound, verbosity=verbosity, commit=commit)


def do_update(backend, index, qs, start, end, total, verbosity=1, commit=True):
    # Get a clone of the QuerySet so that the cache doesn't bloat up
    # in memory. Useful when reindexing large amounts of data.
    small_cache_qs = qs.all()
    current_qs = small_cache_qs[start:end]

    if verbosity >= 2:
        if hasattr(os, 'getppid') and os.getpid() == os.getppid():
            print("  indexed %s - %d of %d." % (start + 1, end, total))
        else:
            print("  indexed %s - %d of %d (by %s)." % (start + 1, end, total, os.getpid()))

    # FIXME: Get the right backend.
    backend.update(index, current_qs, commit=commit)

    # Clear out the DB connections queries because it bloats up RAM.
    reset_queries()


def do_remove(backend, index, model, pks_seen, start, upper_bound, verbosity=1, commit=True):
    # Fetch a list of results.
    # Can't do pk range, because id's are strings (thanks comments
    # & UUIDs!).
    stuff_in_the_index = SearchQuerySet(using=backend.connection_alias).models(model)[start:upper_bound]

    # Iterate over those results.
    for result in stuff_in_the_index:
        # Be careful not to hit the DB.
        if not smart_bytes(result.pk) in pks_seen:
            # The id is NOT in the small_cache_qs, issue a delete.
            if verbosity >= 2:
                print("  removing %s." % result.pk)

            backend.remove(".".join([result.app_label, result.model_name, str(result.pk)]), commit=commit)


class Command(LabelCommand):
    help = "Freshens the index for the given app(s)."
    base_options = (
        make_option('-a', '--age', action='store', dest='age',
            default=DEFAULT_AGE, type='int',
            help='Number of hours back to consider objects new.'
        ),
        make_option('-s', '--start', action='store', dest='start_date',
            default=None, type='string',
            help='The start date for indexing within. Can be any dateutil-parsable string, recommended to be YYYY-MM-DDTHH:MM:SS.'
        ),
        make_option('-e', '--end', action='store', dest='end_date',
            default=None, type='string',
            help='The end date for indexing within. Can be any dateutil-parsable string, recommended to be YYYY-MM-DDTHH:MM:SS.'
        ),
        make_option('-b', '--batch-size', action='store', dest='batchsize',
            default=None, type='int',
            help='Number of items to index at once.'
        ),
        make_option('-r', '--remove', action='store_true', dest='remove',
            default=False, help='Remove objects from the index that are no longer present in the database.'
        ),
        make_option("-u", "--using", action="append", dest="using",
            default=[],
            help='Update only the named backend (can be used multiple times). '
                 'By default all backends will be updated.'
        ),
        make_option('-k', '--workers', action='store', dest='workers',
            default=0, type='int',
            help='Allows for the use multiple workers to parallelize indexing. Requires multiprocessing.'
        ),
        make_option('--nocommit', action='store_false', dest='commit',
            default=True, help='Will pass commit=False to the backend.'
        ),
    )
    option_list = LabelCommand.option_list + base_options

    def handle(self, *items, **options):
        self.verbosity = int(options.get('verbosity', 1))
        self.batchsize = options.get('batchsize', DEFAULT_BATCH_SIZE)
        self.start_date = None
        self.end_date = None
        self.remove = options.get('remove', False)
        self.workers = int(options.get('workers', 0))
        self.commit = options.get('commit', True)

        if sys.version_info < (2, 7):
            warnings.warn('multiprocessing is disabled on Python 2.6 and earlier. '
                          'See https://github.com/toastdriven/django-haystack/issues/1001')
            self.workers = 0

        self.backends = options.get('using')
        if not self.backends:
            self.backends = haystack_connections.connections_info.keys()

        age = options.get('age', DEFAULT_AGE)
        start_date = options.get('start_date')
        end_date = options.get('end_date')

        if age is not None:
            self.start_date = now() - timedelta(hours=int(age))

        if start_date is not None:
            from dateutil.parser import parse as dateutil_parse

            try:
                self.start_date = dateutil_parse(start_date)
            except ValueError:
                pass

        if end_date is not None:
            from dateutil.parser import parse as dateutil_parse

            try:
                self.end_date = dateutil_parse(end_date)
            except ValueError:
                pass

        if not items:
            items = haystack_load_apps()

        return super(Command, self).handle(*items, **options)

    def handle_label(self, label, **options):
        for using in self.backends:
            try:
                self.update_backend(label, using)
            except:
                logging.exception("Error updating %s using %s ", label, using)
                raise

    def update_backend(self, label, using):
        from haystack.exceptions import NotHandled

        backend = haystack_connections[using].get_backend()
        unified_index = haystack_connections[using].get_unified_index()

        if self.workers > 0:
            import multiprocessing

        for model in haystack_get_models(label):
            try:
                index = unified_index.get_index(model)
            except NotHandled:
                if self.verbosity >= 2:
                    print("Skipping '%s' - no index." % model)
                continue

            if self.workers > 0:
                # workers resetting connections leads to references to models / connections getting
                # stale and having their connection disconnected from under them. Resetting before
                # the loop continues and it accesses the ORM makes it better.
                close_old_connections()

            qs = index.build_queryset(using=using, start_date=self.start_date,
                                      end_date=self.end_date)

            total = qs.count()

            if self.verbosity >= 1:
                print(u"Indexing %d %s" % (total, force_text(model._meta.verbose_name_plural)))

            batch_size = self.batchsize or backend.batch_size

            if self.workers > 0:
                ghetto_queue = []

            for start in range(0, total, batch_size):
                end = min(start + batch_size, total)

                if self.workers == 0:
                    do_update(backend, index, qs, start, end, total, verbosity=self.verbosity, commit=self.commit)
                else:
                    ghetto_queue.append(('do_update', model, start, end, total, using, self.start_date, self.end_date, self.verbosity, self.commit))

            if self.workers > 0:
                pool = multiprocessing.Pool(self.workers)
                pool.map(worker, ghetto_queue)
                pool.close()
                pool.join()

            if self.remove:
                if self.start_date or self.end_date or total <= 0:
                    # They're using a reduced set, which may not incorporate
                    # all pks. Rebuild the list with everything.
                    qs = index.index_queryset().values_list('pk', flat=True)
                    pks_seen = set(smart_bytes(pk) for pk in qs)

                    total = len(pks_seen)
                else:
                    pks_seen = set(smart_bytes(pk) for pk in qs.values_list('pk', flat=True))

                if self.workers > 0:
                    ghetto_queue = []

                for start in range(0, total, batch_size):
                    upper_bound = start + batch_size

                    if self.workers == 0:
                        do_remove(backend, index, model, pks_seen, start, upper_bound, verbosity=self.verbosity,  commit=self.commit)
                    else:
                        ghetto_queue.append(('do_remove', model, pks_seen, start, upper_bound, using, self.verbosity, self.commit))

                if self.workers > 0:
                    pool = multiprocessing.Pool(self.workers)
                    pool.map(worker, ghetto_queue)
                    pool.terminate()
