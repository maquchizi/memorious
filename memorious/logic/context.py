import os
import six
import uuid
import shutil
import logging
from copy import deepcopy
from tempfile import mkdtemp
from contextlib import contextmanager

from memorious.core import manager, storage
from memorious.core import datastore
from memorious.model import Tag, Event, Queue, Crawl
from memorious.logic.http import ContextHttp
from memorious.logic.check import ContextCheck
from memorious.util import make_key, random_filename


class Context(object):
    """Provides state tracking and methods for operation interactions."""

    def __init__(self, crawler, stage, state):
        self.crawler = crawler
        self.stage = stage
        self.state = state
        self.params = stage.params
        self.incremental = state.get('incremental')
        self.run_id = state.get('run_id') or uuid.uuid1().hex
        self.work_path = mkdtemp()
        self.log = logging.getLogger('%s.%s' % (crawler.name, stage.name))
        self.http = ContextHttp(self)
        self.datastore = datastore
        self.check = ContextCheck(self)

    def get(self, name, default=None):
        """Get a configuration value and expand environment variables."""
        value = self.params.get(name, default)
        if isinstance(value, six.string_types):
            value = os.path.expandvars(value)
        return value

    def emit(self, rule='pass', stage=None, data={}, delay=None):
        """Invoke the next stage, either based on a handling rule, or by calling
        the `pass` rule by default."""
        if stage is None:
            stage = self.stage.handlers.get(rule)
        if stage is None or stage not in self.crawler.stages:
            self.log.info("No next stage: %s (%s)" % (stage, rule))
            return
        state = self.dump_state()
        delay = delay or self.crawler.delay
        Queue.queue(stage, state, data, delay)

    def recurse(self, data={}, delay=None):
        """Have a stage invoke itself with a modified set of arguments."""
        return self.emit(stage=self.stage.name,
                         data=data,
                         delay=delay)

    def execute(self, data):
        """Execute the crawler and create a database record of having done
        so."""
        if Crawl.is_aborted(self.crawler, self.run_id):
            return

        try:
            Crawl.operation_start(self.crawler,
                                         self.stage,
                                         self.run_id)
            self.log.info('[%s->%s(%s)]: %s',
                          self.crawler.name,
                          self.stage.name,
                          self.stage.method_name,
                          self.run_id)
            return self.stage.method(self, data)
        except Exception as exc:
            self.emit_exception(exc)
        finally:
            Crawl.operation_end(self.crawler, self.run_id)
            shutil.rmtree(self.work_path)

    def emit_warning(self, message, type=None, *args):
        if len(args):
            message = message % args
        self.log.warning(message)
        return Event.save(self.crawler,
                          self.stage,
                          Event.LEVEL_WARNING,
                          self.run_id,
                          error=type,
                          message=message)

    def emit_exception(self, exc):
        self.log.exception(exc)
        return Event.save(self.crawler,
                          self.stage,
                          Event.LEVEL_ERROR,
                          self.run_id,
                          error=exc.__class__.__name__,
                          message=six.text_type(exc))

    def set_tag(self, key, value):
        return Tag.save(self.crawler, key, value)

    def get_tag(self, key):
        return Tag.find(self.crawler, key)

    def check_tag(self, key):
        return Tag.exists(self.crawler, key)

    def skip_incremental(self, *criteria):
        """Perform an incremental check on a set of criteria.

        This can be used to execute a part of a crawler only once per an
        interval (which is specified by the ``expire`` setting). If the
        operation has already been performed (and should thus be skipped),
        this will return ``True``. If the operation needs to be executed,
        the returned value will be ``False``.
        """
        if not self.incremental:
            return False

        # this is pure convenience, and will probably backfire at some point.
        key = make_key(*criteria)
        if key is None:
            return False

        if Tag.exists(self.crawler, key):
            return True

        self.set_tag(key, None)
        return False

    def store_file(self, file_path, content_hash=None):
        """Put a file into permanent storage so it can be visible to other
        stages."""
        return storage.archive_file(file_path, content_hash=content_hash)

    def store_data(self, data, encoding='utf-8'):
        """Put the given content into a file, possibly encoding it as UTF-8
        in the process."""
        path = random_filename(self.work_path)
        try:
            with open(path, 'wb') as fh:
                if isinstance(data, six.text_type):
                    data = data.encode(encoding)
                if data is not None:
                    fh.write(data)
            return self.store_file(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    @contextmanager
    def load_file(self, content_hash, file_name=None, read_mode='rb'):
        file_path = storage.load_file(content_hash,
                                      file_name=file_name,
                                      temp_path=self.work_path)
        if file_path is None:
            yield None
        else:
            try:
                with open(file_path, mode=read_mode) as fh:
                    yield fh
            finally:
                storage.cleanup_file(content_hash,
                                     temp_path=self.work_path)

    def dump_state(self):
        state = deepcopy(self.state)
        state['crawler'] = self.crawler.name
        state['run_id'] = self.run_id
        return state

    @classmethod
    def from_state(cls, state, stage):
        state_crawler = state.get('crawler')
        crawler = manager.get(state_crawler)
        if crawler is None:
            raise RuntimeError("Missing crawler: [%s]" % state_crawler)
        stage = crawler.get(stage)
        if stage is None:
            raise RuntimeError('[%r] has no stage: %s' % (crawler, stage))
        return cls(crawler, stage, state)

    def __repr__(self):
        return '<Context(%r, %r)>' % (self.crawler, self.stage)
