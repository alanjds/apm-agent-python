#  BSD 3-Clause License
#
#  Copyright (c) 2012, the Sentry Team, see AUTHORS for more details
#  Copyright (c) 2019, Elasticsearch BV
#  All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  * Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
#  * Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
#  * Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#  DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
#  FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
#  DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
#  SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
#  CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
#  OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE


from __future__ import absolute_import

import logging
import sys
import traceback

from elasticapm.base import Client
from elasticapm.traces import execution_context
from elasticapm.utils import compat, wrapt
from elasticapm.utils.encoding import to_unicode
from elasticapm.utils.stacks import iter_stack_frames


class LoggingHandler(logging.Handler):
    def __init__(self, *args, **kwargs):
        client = kwargs.pop("client_cls", Client)
        if len(args) == 1:
            arg = args[0]
            args = args[1:]
            if isinstance(arg, Client):
                self.client = arg
            else:
                raise ValueError(
                    "The first argument to %s must be a Client instance, "
                    "got %r instead." % (self.__class__.__name__, arg)
                )
        elif "client" in kwargs:
            self.client = kwargs.pop("client")
        else:
            self.client = client(*args, **kwargs)

        logging.Handler.__init__(self, level=kwargs.get("level", logging.NOTSET))

    def emit(self, record):
        self.format(record)

        # Avoid typical config issues by overriding loggers behavior
        if record.name.startswith(("elasticapm.errors",)):
            sys.stderr.write(to_unicode(record.message) + "\n")
            return

        try:
            return self._emit(record)
        except Exception:
            sys.stderr.write("Top level ElasticAPM exception caught - failed creating log record.\n")
            sys.stderr.write(to_unicode(record.msg + "\n"))
            sys.stderr.write(to_unicode(traceback.format_exc() + "\n"))

            try:
                self.client.capture("Exception")
            except Exception:
                pass

    def _emit(self, record, **kwargs):
        data = {}

        for k, v in compat.iteritems(record.__dict__):
            if "." not in k and k not in ("culprit",):
                continue
            data[k] = v

        stack = getattr(record, "stack", None)
        if stack is True:
            stack = iter_stack_frames()

        if stack:
            frames = []
            started = False
            last_mod = ""
            for item in stack:
                if isinstance(item, (list, tuple)):
                    frame, lineno = item
                else:
                    frame, lineno = item, item.f_lineno

                if not started:
                    f_globals = getattr(frame, "f_globals", {})
                    module_name = f_globals.get("__name__", "")
                    if last_mod.startswith("logging") and not module_name.startswith("logging"):
                        started = True
                    else:
                        last_mod = module_name
                        continue
                frames.append((frame, lineno))
            stack = frames

        custom = getattr(record, "data", {})
        # Add in all of the data from the record that we aren't already capturing
        for k in record.__dict__.keys():
            if k in (
                "stack",
                "name",
                "args",
                "msg",
                "levelno",
                "exc_text",
                "exc_info",
                "data",
                "created",
                "levelname",
                "msecs",
                "relativeCreated",
            ):
                continue
            if k.startswith("_"):
                continue
            custom[k] = record.__dict__[k]

        # If there's no exception being processed,
        # exc_info may be a 3-tuple of None
        # http://docs.python.org/library/sys.html#sys.exc_info
        if record.exc_info and all(record.exc_info):
            handler = self.client.get_handler("elasticapm.events.Exception")
            exception = handler.capture(self.client, exc_info=record.exc_info)
        else:
            exception = None

        return self.client.capture(
            "Message",
            param_message={"message": compat.text_type(record.msg), "params": record.args},
            stack=stack,
            custom=custom,
            exception=exception,
            level=record.levelno,
            logger_name=record.name,
            **kwargs
        )


class LoggingFilter(logging.Filter):
    """
    This filter doesn't actually do any "filtering" -- rather, it just adds
    three new attributes to any "filtered" LogRecord objects:

    * elasticapm_transaction_id
    * elasticapm_trace_id
    * elasticapm_span_id

    These attributes can then be incorporated into your handlers and formatters,
    so that you can tie log messages to transactions in elasticsearch.

    This filter also adds these fields to a dictionary attribute,
    `elasticapm_labels`, using the official tracing fields names as documented
    here: https://www.elastic.co/guide/en/ecs/current/ecs-tracing.html

    Note that if you're using Python 3.2+, by default we will add a
    LogRecordFactory to your root logger which will add these attributes
    automatically.
    """

    def filter(self, record):
        """
        Add elasticapm attributes to `record`.
        """
        _add_attributes_to_log_record(record)
        return True


@wrapt.decorator
def log_record_factory(wrapped, instance, args, kwargs):
    """
    Decorator, designed to wrap the python log record factory (fetched by
    logging.getLogRecordFactory), adding the same custom attributes as in
    the LoggingFilter provided above.

    :return:
        LogRecord object, with custom attributes for APM tracing fields
    """
    record = wrapped(*args, **kwargs)
    return _add_attributes_to_log_record(record)


def _add_attributes_to_log_record(record):
    """
    Add custom attributes for APM tracing fields to a LogRecord object

    :param record: LogRecord object
    :return: Updated LogRecord object with new APM tracing fields
    """
    transaction = execution_context.get_transaction()

    transaction_id = transaction.id if transaction else None
    record.elasticapm_transaction_id = transaction_id

    trace_id = transaction.trace_parent.trace_id if transaction and transaction.trace_parent else None
    record.elasticapm_trace_id = trace_id

    span = execution_context.get_span()
    span_id = span.id if span else None
    record.elasticapm_span_id = span_id

    record.elasticapm_labels = {"transaction.id": transaction_id, "trace.id": trace_id, "span.id": span_id}

    return record
