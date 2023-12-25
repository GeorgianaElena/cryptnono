#!/usr/bin/env python3
# Copied from * Copied from https://github.com/iovisor/bcc/blob/b57dbb397cb110433c743685a7d1eb1fb9c3b1f9/tools/execsnoop.py
# and modified to kill processes, rather than just log them

import re
import argparse
from concurrent.futures import Executor, ThreadPoolExecutor
from enum import Enum
import json
from logging import DEBUG, INFO
import os
from psutil import NoSuchProcess, process_iter
import signal
import structlog
import threading
import time
from functools import partial
from collections import defaultdict
from shlex import join

import ahocorasick
from bcc import BPF
from lookup_container import ContainerNotFound, get_cri_container_id, lookup_container_details_crictl
from prometheus_client import Counter, start_http_server


# Lazily initialised with configuration on first use
logging = structlog.get_logger()


class EventType:
    """
    Type of event dispatched from eBPF.

    Matches the `event_type` enum in execwhacker.bpf.c.
    """

    # Pass a single argument from eBPF to python
    EVENT_ARG = 0
    # Pass a return value from eBPF to python
    EVENT_RET = 1


class ProcessSource(Enum):
    """
    Where did execwhacker get the process information from?
    """
    BPF = "execwhacker.bpf"
    SCAN = "psutil.process_iter"


# ahocorasick is not thread-safe, so just in case use a lock
# https://github.com/WojciechMula/pyahocorasick/issues/114
banned_strings_automaton_lock = threading.Lock()

# Optionally override this in development to avoid polluting real metrics
cryptnono_metrics_prefix = os.getenv("CRYPTNONO_METRICS_PREFIX", "cryptnono")

processes_checked = Counter(f"{cryptnono_metrics_prefix}_execwhacker_processes_checked_total", "Total number of processes checked", ["source"])
processes_killed = Counter(f"{cryptnono_metrics_prefix}_execwhacker_processes_killed_total", "Total number of processes killed", ["source"])


def log_and_kill(pid, cmdline, b, source, lookup_container):
    """
    Attempt to lookup the container details for a given PID, then log and kill it

    This makes an external blocking call to lookup the container details

    Returns True if the process was killed, False if it was not found
    """
    # TODO: Should we switch to structured JSON logging instead?

    cid = None
    pod_name = container_name = container_image = "unknown"

    if lookup_container:
        try:
            cid, cgroupline = get_cri_container_id(pid)
        except ContainerNotFound as e:
            logging.info(f"action:container-lookup-failed pid:{pid} {e}")
            cid = None
        if cid:
            try:
                pod_name, container_name, container_image = lookup_container_details_crictl(cid)
            except ContainerNotFound as e:
                logging.info(f"action:container-lookup-failed pid:{pid} cgroupline:{cgroupline} {e}")

        log_metadata = f"pid:{pid} cmdline:{cmdline} matched:{b} source:{source.value} pod_name:{pod_name} container_name:{container_name} container_image:{container_image}"
    else:
        log_metadata = f"pid:{pid} cmdline:{cmdline} matched:{b} source:{source.value}"

    try:
        os.kill(pid, signal.SIGKILL)
        logging.info(f"action:killed {log_metadata}")
        inc_args = {}
        if pod_name != "unknown":
            # prometheus supports "exemplars" which can be attached to a metric:
            # https://prometheus.github.io/client_python/instrumenting/exemplars/
            # https://grafana.com/docs/grafana/latest/fundamentals/exemplars/
            #
            # curl -H 'Accept: application/openmetrics-text' localhost:12121/metrics
            inc_args["exemplar"] = {"pod_name": pod_name, "container_image": container_image}
        processes_killed.labels(source=source.value).inc(**inc_args)
        return True
    except ProcessLookupError:
        logging.info(
            f"action:missed-kill {log_metadata} Process exited before we could kill it"
        )


def kill_if_needed(banned_strings_automaton, allowed_patterns, cmdline, pid, source, executor, lookup_container):
    """
    Kill given process (pid) with cmdline if appropriate, based on banned_command_strings

    The kill is run in a separate thread since it involves a blocking call to attempt
    to lookup the container details

    returns: Future object that evaluates to True if the process was killed
    """
    log = logging.bind(pid=pid, cmdline=cmdline, source=source.value)

    # Make all matches be case insensitive
    cmdline = cmdline.casefold()
    with banned_strings_automaton_lock:
        processes_checked.labels(source=source.value).inc()
        for _, b in banned_strings_automaton.iter(cmdline):
            for ap in allowed_patterns:
                if re.match(ap, cmdline, re.IGNORECASE):
                    if source != ProcessSource.SCAN:
                        log.info("Not killing process", action="spared", matched=b, allowedby=ap)
                    return
            # Only kill if the banned string is a standalone "word",
            # i.e. it's surrounded by whitespace, punctuation, etc.
            if (
                    re.search(r"\w" + re.escape(b), cmdline, re.IGNORECASE) or
                    re.search(re.escape(b) + r"\w", cmdline, re.IGNORECASE)
            ):
                log.info("Not killing process", action="spared", matched=b, allowedby="is-substring")
                return
            future = executor.submit(log_and_kill, pid, cmdline, b, source, lookup_container)
            return future


def process_event(
    b: BPF,
    argv: dict,
    banned_strings_automaton: ahocorasick.Automaton,
    allowed_patterns: list,
    executor: Executor,
    lookup_container: bool,
    ctx,
    data,
    size,
):
    """
    Callback each time an event is sent from eBPF to python
    """
    event = b["events"].event(data)

    if event.type == EventType.EVENT_ARG:
        # We are getting a single argument passed in for this pid
        # Save it into a temporary dict so we can construct the whole
        # argv for a pid, event by event.
        argv[event.pid].append(event.argv.decode())
    elif event.type == EventType.EVENT_RET:
        # The exec call itself has returned, but the process has
        # not. This means we have the full set of args now.
        cmdline = join(argv[event.pid])
        start_time = time.perf_counter()
        kill_if_needed(banned_strings_automaton, allowed_patterns, cmdline, event.pid, ProcessSource.BPF, executor, lookup_container)
        duration = time.perf_counter() - start_time

        log = logging.bind(pid=event.pid, cmdline=cmdline)
        log.debug("New process", action="observed", duration=f"{duration:0.10f}s")

        try:
            # Cleanup our dict, as we're no longer collecting args
            # via the ring buffer
            del argv[event.pid]
        except Exception as e:
            # Catch any possible exception here - either a KeyError from
            # argv not containing `event.pid` or something from ctypes as we
            # try to access `event.pid`. *Should* not happen, but better than
            # crashing? Also this was in the bcc execsnoop code, so it stays here.
            # Brendan knows best
            log.exception(e)


def check_existing_processes(banned_strings_automaton, allowed_patterns, interval, executor, lookup_container):
    """
    Scan all running processes for banned strings
    
    BPF only looks for new processes, this will find banned processes that were already
    running, and any that might've been missed by BPF for unknown reasons.
    """
    while True:
        for proc in process_iter():
            try:
                if proc.exe():
                    kill_if_needed(
                        banned_strings_automaton,
                        allowed_patterns,
                        join(proc.cmdline()),
                        proc.pid,
                        ProcessSource.SCAN,
                        executor,
                        lookup_container,
                    )
            except NoSuchProcess as e:
                logging.info(e, action="process-already-exited", pid=proc.pid, source=ProcessSource.SCAN.value)
        time.sleep(interval)


def main():
    start_time = time.perf_counter()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-args",
        default="128",
        help="maximum number of arguments parsed and displayed, defaults to 128",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--config",
        help="JSON config file listing what processes to snipe",
        action="append",
        default=[],
    )
    parser.add_argument("--scan-existing", type=int, default=600, help="Scan all existing processes at this interval (seconds), set to 0 to disable")

    # Currently metrics are served on any path under / since this is what
    # start_http_server does by default, but we may want to change
    # this in the future so only /metrics is supported
    parser.add_argument("--serve-metrics-port", type=int, default=0, help="Serve prometheus metrics on this port under /metrics, set to 0 to disable")

    parser.add_argument("--lookup-container", action="store_true", help="Attempt to lookup the container details for a process before killing it")

    args = parser.parse_args()

    # https://www.structlog.org/en/stable/standard-library.html
    # https://www.structlog.org/en/stable/performance.html
    structlog.configure(
        cache_logger_on_first_use=True,
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.dict_tracebacks,
            structlog.processors.ExceptionRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(DEBUG if args.debug else INFO),
    )

    # Initialise prometheus counters to 0 so they show up straight away
    # instead of only after the first process is killed
    for source in ProcessSource:
        for counter in [processes_checked, processes_killed]:
            counter.labels(source=source.value)

    banned_strings = set()
    allowed_patterns = []
    for config_file in args.config:
        with open(config_file) as f:
            config_file_contents = json.load(f)
            banned_strings.update(config_file_contents.get("bannedCommandStrings", []))

            allowed_patterns += config_file_contents.get("allowedCommandPatterns", [])

    # Use the Aho Corasick algorithm (https://en.wikipedia.org/wiki/Aho%E2%80%93Corasick_algorithm)
    # for *very* fast searching, given we always have only one cmdline but potentially tens of thousands
    # of banned substrings to search for inside it. This is run *every time a process spawns*, so should
    # be quick. Compared to a naive solution using set() and 'in', this is
    # roughly 60-100x faster, and uses a lot less CPU too!
    # Credit to Michael Rothwell on Mastodon for pointing me to this direction!
    # https://hachyderm.io/@mrothwell/111317806566634439
    banned_strings_automaton = ahocorasick.Automaton()
    for b in banned_strings:
        # casefold banned strings so we can do case insensitive matching
        banned_strings_automaton.add_word(b.casefold(), b.casefold())

    banned_strings_automaton.make_automaton()

    logging.info(
        f"Found {len(banned_strings)} substrings to check process cmdlines for"
    )
    if len(banned_strings) == 0:
        logging.warning("WARNING: No substrings to whack have been specified, so execwhacker is useless! Check your config?")

    logging.info(f"Found {len(allowed_patterns)} patterns to explicitly allow")

    # initialize BPF
    logging.info("Compiling and loading BPF program...")
    with open(os.path.join(os.path.dirname(__file__), "execwhacker.bpf.c")) as f:
        bpf_text = f.read()

    # FIXME: Investigate what exactly happens when this is different
    bpf_text = bpf_text.replace("MAXARG", args.max_args)
    b = BPF(text=bpf_text)
    execve_fnname = b.get_syscall_fnname("execve")
    b.attach_kprobe(event=execve_fnname, fn_name="syscall__execve")
    b.attach_kretprobe(event=execve_fnname, fn_name="do_ret_sys_execve")

    argv = defaultdict(list)
    executor = ThreadPoolExecutor()

    # Trigger our callback each time something is written to the
    # "events" ring buffer. We use a partial to pass in appropriate 'global'
    # context that's common to all callbacks instead of defining our callback
    # as an inline function and use closures, to keep things clean and hopefully
    # unit-testable in the future.
    b["events"].open_ring_buffer(
        partial(process_event, b, argv, banned_strings_automaton, allowed_patterns, executor, args.lookup_container)
    )

    startup_duration = time.perf_counter() - start_time
    logging.info(f"Took {startup_duration:0.2f}s to startup")

    if args.serve_metrics_port:
        start_http_server(args.serve_metrics_port)

    if args.scan_existing:
        # Only run this after the BPF events are being captured, to avoid
        # processes slipping past
        t = threading.Thread(
            target=check_existing_processes,
            args=(banned_strings_automaton, allowed_patterns, args.scan_existing, executor, args.lookup_container),
        )
        t.daemon = True
        t.start()
        # Don't care about waiting for thread to finish

    logging.info("Watching for processes we don't like...")
    while 1:
        try:
            b.ring_buffer_poll()
        except KeyboardInterrupt:
            exit()


if __name__ == "__main__":
    main()
