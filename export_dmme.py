#!/usr/bin/env python3
"""Capture decrypted book resources from the installed DMMbookviewer."""
from __future__ import annotations

import argparse
import hashlib
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

VIEWER_SHA256 = "edfac9ac051fdb6726dcc77168d661f546c062e64b3e05af405f2b2bf71cfd5f"
LOAD_JOB_READ_RAW_RVA = 0x8B340
# The tail-jump displacement is link-dependent; the invariant prefix ends at E9.
LOAD_JOB_READ_RAW_SIGNATURE = "45 89 01 48 8B 89 18 01 00 00 4D 8B C1 E9"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PAGE_RESOURCE_SOURCE = "qt.QPixmap.loadFromData"
MIN_PAGE_AREA = 500_000

# Qt QIODevice is the active book path in this viewer build. The URLRequest
# hooks remain useful for a build or resource that uses Chromium's loader.
JS = r"""
'use strict';

const MAX_CHUNK = 64 * 1024 * 1024;
let callSequence = 0;
let navigationPage = -1;
const installedReads = new Set();
const installedBoundaries = new Set();
const main = Process.mainModule;

function findExport(name) {
  const module = Process.findModuleByName('net.dll');
  return module ? module.findExportByName(name) : null;
}

function findModuleExport(moduleName, name) {
  const module = Process.findModuleByName(moduleName);
  return module ? module.findExportByName(name) : null;
}

function installRead(name, target) {
  if (!target) {
    send({type: 'hook-missing', name: name});
    return false;
  }
  const key = target.toString();
  if (installedReads.has(key)) return true;
  installedReads.add(key);
  try {
    Interceptor.attach(target, {
      onEnter(args) {
        this.buffer = args[1];
        this.capacity = args[2].toInt32();
        this.owner = args[0].toString();
        this.sequence = callSequence++;
      },
      onLeave(retval) {
        try {
          const n = retval.toInt32();
          if (n > 0 && n <= MAX_CHUNK && !this.buffer.isNull()) {
            send({
              type: 'resource-chunk',
              source: name,
              owner: this.owner,
              sequence: this.sequence,
              requested: this.capacity,
              size: n
            }, this.buffer.readByteArray(n));
          } else if (n <= 0) {
            send({
              type: 'resource-eof',
              source: name,
              owner: this.owner,
              sequence: this.sequence
            });
          }
        } catch (e) {
          send({type: 'hook-error', name: name, error: String(e)});
        }
      }
    });
    send({type: 'hook-installed', name: name, address: target.toString()});
    return true;
  } catch (e) {
    send({type: 'hook-error', name: name, error: String(e)});
    return false;
  }
}

function installLoadJobRead() {
  const rva = __LOAD_JOB_RVA__;
  if (rva !== null) {
    const target = main.base.add(rva);
    send({type: 'load-job-resolved', mode: 'rva', matches: 1,
          address: target.toString()});
    return installRead('load_job.ReadRawData', target);
  }
  try {
    const matches = [];
    for (const range of main.enumerateRanges('r-x')) {
      for (const hit of Memory.scanSync(
        range.base, range.size, '__LOAD_JOB_SIGNATURE__'
      )) {
        try {
          const address = hit.address;
          const destination = address.add(18).add(address.add(14).readS32());
          const destinationRange = Process.findRangeByAddress(destination);
          const destinationModule = Process.findModuleByAddress(destination);
          if (destinationRange && destinationRange.protection.indexOf('x') !== -1 &&
              destinationModule && destinationModule.name === main.name) {
            matches.push(address);
          }
        } catch (_) {}
      }
    }
    send({type: 'load-job-resolved', mode: 'signature',
          matches: matches.length,
          addresses: matches.map(function(address) { return address.toString(); })});
    if (matches.length !== 1) return false;
    return installRead('load_job.ReadRawData', matches[0]);
  } catch (e) {
    send({type: 'load-job-resolved', mode: 'signature', matches: 0,
          error: String(e)});
    send({type: 'hook-error', name: 'load_job.ReadRawData', error: String(e)});
    return false;
  }
}

function installURLRead(name, target) {
  if (!target) {
    send({type: 'hook-missing', name: name});
    return false;
  }
  const key = target.toString();
  if (installedReads.has(key)) return true;
  installedReads.add(key);
  try {
    Interceptor.attach(target, {
      onEnter(args) {
        this.buffer = args[1];
        this.capacity = args[2].toInt32();
        this.bytesRead = args[3];
        this.owner = args[0].toString();
        this.sequence = callSequence++;
      },
      onLeave(retval) {
        try {
          const n = this.bytesRead.readS32();
          if (n > 0 && n <= MAX_CHUNK && this.buffer && !this.buffer.isNull()) {
            const data = this.buffer.add(Process.pointerSize).readPointer();
            if (data && !data.isNull()) {
              send({
                type: 'resource-chunk',
                source: name,
                owner: this.owner,
                sequence: this.sequence,
                requested: this.capacity,
                size: n
              }, data.readByteArray(n));
            }
          }
          if (retval.toInt32() === 0) {
            send({
              type: 'resource-eof',
              source: name,
              owner: this.owner,
              sequence: this.sequence
            });
          }
        } catch (e) {
          send({type: 'hook-error', name: name, error: String(e)});
        }
      }
    });
    send({type: 'hook-installed', name: name, address: target.toString()});
    return true;
  } catch (e) {
    send({type: 'hook-error', name: name, error: String(e)});
    return false;
  }
}

function installByteArray(name, target, sizeFn, dataFn) {
  if (!target || !sizeFn || !dataFn) {
    send({type: 'hook-missing', name: name});
    return false;
  }
  const key = target.toString();
  if (installedReads.has(key)) return true;
  installedReads.add(key);
  try {
    Interceptor.attach(target, {
      onEnter(args) {
        try {
          const size = sizeFn(args[1]);
          const bytes = dataFn(args[1]);
          if (size > 0 && size <= MAX_CHUNK && bytes && !bytes.isNull()) {
            this.owner = name + ':' + args[0].toString();
            this.page = navigationPage;
            this.sequence = callSequence++;
            navigationLastResourceAt = Date.now();
            send({
              type: 'resource-chunk',
              source: name,
              owner: this.owner,
              sequence: this.sequence,
              page: this.page,
              requested: size,
              size: size
            }, bytes.readByteArray(size));
          }
        } catch (e) {
          send({type: 'hook-error', name: name, error: String(e)});
        }
      },
      onLeave(retval) {
        if (this.owner) {
          send({
            type: 'resource-eof',
            source: name,
            owner: this.owner,
            sequence: this.sequence,
            page: this.page
          });
        }
      }
    });
    send({type: 'hook-installed', name: name, address: target.toString()});
    return true;
  } catch (e) {
    send({type: 'hook-error', name: name, error: String(e)});
    return false;
  }
}

function installBoundary(name, target, type) {
  if (!target) {
    send({type: 'hook-missing', name: name});
    return false;
  }
  const key = type + ':' + target.toString();
  if (installedBoundaries.has(key)) return true;
  installedBoundaries.add(key);
  try {
    Interceptor.attach(target, {
      onEnter(args) {
        send({
          type: type,
          source: name,
          owner: args[0].toString(),
          sequence: callSequence++
        });
      }
    });
    send({type: 'hook-installed', name: name, address: target.toString()});
    return true;
  } catch (e) {
    send({type: 'hook-error', name: name, error: String(e)});
    return false;
  }
}

function installNetHooks() {
  if (!Process.findModuleByName('net.dll')) return false;
  installURLRead('net.URLRequestJob.ReadRawData', findExport(
    '?ReadRawData@URLRequestJob@net@@MEAA_NPEAVIOBuffer@2@HPEAH@Z'
  ));
  installURLRead('net.URLRequest.Read', findExport(
    '?Read@URLRequest@net@@QEAA_NPEAVIOBuffer@2@HPEAH@Z'
  ));
  return true;
}

function installQtHooks() {
  const core = Process.findModuleByName('Qt5Core.dll');
  const gui = Process.findModuleByName('Qt5Gui.dll');
  if (!core || !gui) return false;

  installRead('qt.QFileDevice.readData', core.findExportByName(
    '?readData@QFileDevice@@MEAA_JPEAD_J@Z'
  ));
  installRead('qt.QBuffer.readData', core.findExportByName(
    '?readData@QBuffer@@MEAA_JPEAD_J@Z'
  ));
  installBoundary('qt.QFile.open', core.findExportByName(
    '?open@QFile@@UEAA_NV?$QFlags@W4OpenModeFlag@QIODevice@@@@@Z'
  ), 'resource-open');
  installBoundary('qt.QIODevice.open', core.findExportByName(
    '?open@QIODevice@@UEAA_NV?$QFlags@W4OpenModeFlag@QIODevice@@@@@Z'
  ), 'resource-open');
  installBoundary('qt.QFileDevice.close', core.findExportByName(
    '?close@QFileDevice@@UEAAXXZ'
  ), 'resource-close');

  const byteArraySizeAddress = core.findExportByName('?size@QByteArray@@QEBAHXZ');
  const byteArrayDataAddress = core.findExportByName('?constData@QByteArray@@QEBAPEBDXZ');
  const byteArraySize = byteArraySizeAddress
    ? new NativeFunction(byteArraySizeAddress, 'int', ['pointer'])
    : null;
  const byteArrayData = byteArrayDataAddress
    ? new NativeFunction(byteArrayDataAddress, 'pointer', ['pointer'])
    : null;
  installByteArray('qt.QPixmap.loadFromData', findModuleExport(
    'Qt5Gui.dll',
    '?loadFromData@QPixmap@@QEAA_NAEBVQByteArray@@PEBDV?$QFlags@W4ImageConversionFlag@Qt@@@@@Z'
  ), byteArraySize, byteArrayData);
  send({type: 'qt-hooks-ready'});
  return true;
}

function installPageNavigation() {
  if (!navigationEnabled || navigationInstalled) return;
  const core = Process.findModuleByName('Qt5Core.dll');
  const widgets = Process.findModuleByName('Qt5Widgets.dll');
  const quickWidgets = Process.findModuleByName('Qt5QuickWidgets.dll');
  if (!core || !widgets || !quickWidgets) return;
  try {
    const topAddress = widgets.findExportByName(
      '?topLevelWidgets@QApplication@@SA?AV?$QList@PEAVQWidget@@@@XZ');
    const rootAddress = quickWidgets.findExportByName(
      '?rootObject@QQuickWidget@@QEBAPEAVQQuickItem@@XZ');
    const childrenAddress = core.findExportByName(
      '?children@QObject@@QEBAAEBV?$QList@PEAVQObject@@@@XZ');
    const inheritsAddress = core.findExportByName(
      '?inherits@QObject@@QEBA_NPEBD@Z');
    const metaAddress = core.findExportByName(
      '?metaObject@QObject@@UEBAPEBUQMetaObject@@XZ');
    const metacallAddress = core.findExportByName(
      '?metacall@QMetaObject@@SAHPEAVQObject@@W4Call@1@HPEAPEAX@Z');
    const indexOfPropertyAddress = core.findExportByName(
      '?indexOfProperty@QMetaObject@@QEBAHPEBD@Z');
    const invokeMethodAddress = core.findExportByName(
      '?invokeMethod@QMetaObject@@SA_NPEAVQObject@@PEBDW4ConnectionType@Qt@@VQGenericArgument@@333333333@Z');
    if (!topAddress || !rootAddress || !childrenAddress || !inheritsAddress ||
        !metaAddress || !metacallAddress || !indexOfPropertyAddress || !invokeMethodAddress) {
      send({type: 'navigation-missing'});
      return;
    }
    navTopLevelWidgets = new NativeFunction(topAddress, 'pointer', ['pointer']);
    navRootObject = new NativeFunction(rootAddress, 'pointer', ['pointer']);
    navChildren = new NativeFunction(childrenAddress, 'pointer', ['pointer']);
    navInherits = new NativeFunction(inheritsAddress, 'bool', ['pointer', 'pointer']);
    navMetaObject = new NativeFunction(metaAddress, 'pointer', ['pointer']);
    navMetacall = new NativeFunction(metacallAddress, 'int', ['pointer', 'int', 'int', 'pointer']);
    navIndexOfProperty = new NativeFunction(indexOfPropertyAddress, 'int', ['pointer', 'pointer']);
    navInvokeMethod = new NativeFunction(invokeMethodAddress, 'bool', [
      'pointer', 'pointer', 'int',
      'pointer', 'pointer', 'pointer', 'pointer', 'pointer',
      'pointer', 'pointer', 'pointer', 'pointer', 'pointer'
    ]);
    navigationInstalled = true;
    send({type: 'navigation-ready'});
  } catch (e) {
    send({type: 'navigation-error', error: String(e)});
  }
}

function navigationListPointers(list) {
  const result = [];
  try {
    const data = list.readPointer();
    if (data.isNull()) return result;
    const begin = data.add(8).readS32();
    const end = data.add(12).readS32();
    if (begin < 0 || end < begin || end - begin > 10000) return result;
    for (let i = begin; i < end; i++) {
      const value = data.add(16 + i * Process.pointerSize).readPointer();
      if (!value.isNull()) result.push(value);
    }
  } catch (_) {}
  return result;
}
function navigationChildren(obj) {
  try { return navigationListPointers(navChildren(obj)); } catch (_) { return []; }
}
function navigationIsType(obj, name) {
  try { return navInherits(obj, Memory.allocUtf8String(name)); } catch (_) { return false; }
}
function navigationFindCanvas(obj, seen, depth) {
  if (!obj || obj.isNull() || depth > 25 || seen.has(obj.toString())) return null;
  seen.add(obj.toString());
  if (navigationIsType(obj, 'PageCanvas')) return obj;
  for (const child of navigationChildren(obj)) {
    const found = navigationFindCanvas(child, seen, depth + 1);
    if (found) return found;
  }
  if (navigationIsType(obj, 'QQuickWidget')) {
    try {
      return navigationFindCanvas(navRootObject(obj), seen, depth + 1);
    } catch (_) {}
  }
  return null;
}
function navigationLocateCanvas() {
  const list = Memory.alloc(8);
  navTopLevelWidgets(list);
  const seen = new Set();
  for (const widget of navigationListPointers(list)) {
    const found = navigationFindCanvas(widget, seen, 0);
    if (found) return found;
  }
  return null;
}
function navigationPropertyIndex(name) {
  return navIndexOfProperty(navMetaObject(navigationCanvas), Memory.allocUtf8String(name));
}
function navigationReadProperty(index) {
  if (index < 0) return -1;
  navigationValue.writeS32(0);
  navigationArgv.writePointer(navigationValue);
  navMetacall(navigationCanvas, 1, index, navigationArgv);
  return navigationValue.readS32();
}
function navigationInvoke(page) {
  navigationPage = page;
  const value = Memory.alloc(4);
  value.writeS32(page);
  const argument = Memory.alloc(16);
  argument.writePointer(value); // QGenericArgument::_data
  argument.add(8).writePointer(navigationPageArgumentName); // _name
  navigationArguments.push({value: value, argument: argument});
  return navInvokeMethod(
    navigationCanvas, navigationPageJumpName, 2, argument,
    navigationEmptyArguments[0], navigationEmptyArguments[1],
    navigationEmptyArguments[2], navigationEmptyArguments[3],
    navigationEmptyArguments[4], navigationEmptyArguments[5],
    navigationEmptyArguments[6], navigationEmptyArguments[7],
    navigationEmptyArguments[8]
  );
}
function navigationMakeOrder(count) {
  const result = [];
  for (let i = 0; i < count; i++) result.push(i);
  return result;
}
function navigationTick() {
  try {
    if (!navigationEnabled || navigationDone) return;
    if (!navigationInstalled) {
      installPageNavigation();
      return;
    }
    if (!navigationCanvas) {
      navigationCanvas = navigationLocateCanvas();
      if (!navigationCanvas) return;
      navigationPageCountIndex = navigationPropertyIndex('pageCount');
      navigationCurrentPageIndex = navigationPropertyIndex('currentPage');
      if (navigationPageCountIndex < 0 || navigationCurrentPageIndex < 0) {
        send({type: 'navigation-error', error: 'PageCanvas properties not found'});
        navigationDone = true;
        return;
      }
      send({type: 'navigation-canvas', address: navigationCanvas.toString()});
    }
    const count = navigationReadProperty(navigationPageCountIndex);
    if (count <= 0) return;
    if (!navigationOrderPages) {
      navigationOrderPages = navigationMakeOrder(count);
      navigationNextPage = navigationOrderPages.shift();
      send({type: 'navigation-properties', pageCount: count,
            currentPage: navigationReadProperty(navigationCurrentPageIndex),
            order: navigationOrder, count: count});
    }
    const current = navigationReadProperty(navigationCurrentPageIndex);
    if (navigationPendingPage !== null) {
      if (current === navigationPendingPage) {
        if (!navigationReadyAt) navigationReadyAt = Date.now();
        if (Date.now() - navigationReadyAt >= navigationWaitMs &&
            Date.now() - navigationLastResourceAt >= 200) {
          if (!navigationOrderPages.length) {
            navigationDone = true;
            send({type: 'navigation-done', pageCount: count});
            return;
          }
          navigationNextPage = navigationOrderPages.shift();
          navigationPendingPage = null;
          navigationReadyAt = 0;
        }
      } else if (Date.now() - navigationSentAt > 8000) {
        const queued = navigationInvoke(navigationPendingPage);
        navigationSentAt = Date.now();
        send({type: 'navigation-retry', page: navigationPendingPage,
              current: current, queued: queued});
      }
    }
    if (navigationPendingPage === null && navigationNextPage !== null) {
      const page = navigationNextPage;
      const queued = navigationInvoke(page);
      navigationPendingPage = page;
      navigationNextPage = null;
      navigationSentAt = Date.now();
      navigationReadyAt = 0;
      send({type: 'navigation-jump', page: page, current: current, queued: queued});
    }
  } catch (e) {
    send({type: 'navigation-error', error: String(e)});
    navigationDone = true;
  }
}

let navigationEnabled = __TRAVERSE__;
const navigationOrder = 'forward';
const navigationWaitMs = __NAV_WAIT_MS__;
let navigationInstalled = false;
let navigationDone = false;
let navigationCanvas = null;
let navigationPageCountIndex = -1;
let navigationCurrentPageIndex = -1;
let navigationOrderPages = null;
let navigationNextPage = null;
let navigationPendingPage = null;
let navigationSentAt = 0;
let navigationReadyAt = 0;
let navigationLastResourceAt = 0;
let navTopLevelWidgets = null;
let navRootObject = null;
let navChildren = null;
let navInherits = null;
let navMetaObject = null;
let navMetacall = null;
let navIndexOfProperty = null;
let navInvokeMethod = null;
const navigationValue = Memory.alloc(4);
const navigationArgv = Memory.alloc(16);
const navigationPageArgumentName = Memory.allocUtf8String('int');
const navigationPageJumpName = Memory.allocUtf8String('pageJump');
const navigationArguments = [];
const navigationEmptyArguments = [];
for (let i = 0; i < 9; i++) {
  const empty = Memory.alloc(16);
  empty.writePointer(ptr(0));
  empty.add(8).writePointer(ptr(0));
  navigationEmptyArguments.push(empty);
}
if (navigationEnabled) setInterval(navigationTick, 200);

send({
  type: 'capture-ready',
  mode: '__MODE__',
  module: main.name,
  base: main.base.toString(),
  traverse: navigationEnabled,
  order: navigationOrder
});
installLoadJobRead();
let netReady = installNetHooks();
let qtReady = installQtHooks();
if (!netReady || !qtReady) {
  const timer = setInterval(function() {
    if (!netReady) netReady = installNetHooks();
    if (!qtReady) qtReady = installQtHooks();
    if (netReady && qtReady) clearInterval(timer);
  }, 100);
}
send({type: 'hooks-ready'});
"""


@dataclass(frozen=True)
class CapturedResource:
    path: Path
    sequence: int
    sha256: str
    size: int
    source: str
    page: int = -1


@dataclass
class _ResourceState:
    key: str
    path: Path
    sequence: int
    source: str
    handle: Any
    digest: Any
    page: int = -1
    size: int = 0


@dataclass
class NavigationState:
    done: threading.Event
    page_count: int | None = None
    initial_page: int | None = None
    load_job_matches: int | None = None
    load_job_hooked: bool | None = None
    jumps: list[int] = field(default_factory=list)


class ResourceWriter:
    """Accept Frida messages without blocking the viewer's read callback."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._items: queue.Queue[Any] = queue.Queue()
        self._states: list[_ResourceState] = []
        self._active: dict[str, _ResourceState] = {}
        self._errors: list[str] = []
        self._lock = threading.Lock()
        self._chunks = 0
        self._bytes = 0
        self._last_message = time.monotonic()
        self._worker = threading.Thread(target=self._run, name="resource-writer")
        self._worker.start()

    def _key(self, message: dict[str, Any]) -> str:
        return f"{message['source']}:{message['owner']}"

    def submit(self, message: dict[str, Any], data: bytes | bytearray | memoryview) -> None:
        payload = bytes(data)
        if not payload:
            return
        with self._lock:
            self._chunks += 1
            self._bytes += len(payload)
            self._last_message = time.monotonic()
        self._items.put(("chunk", message, payload))

    def submit_boundary(self, kind: str, message: dict[str, Any]) -> None:
        with self._lock:
            self._last_message = time.monotonic()
        self._items.put((kind, message, None))

    @property
    def chunks(self) -> int:
        with self._lock:
            return self._chunks

    @property
    def bytes_written(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def last_message(self) -> float:
        with self._lock:
            return self._last_message

    def _new_state(self, message: dict[str, Any]) -> _ResourceState:
        sequence = int(message["sequence"])
        path = self.root / f"resource_{sequence:08d}_{len(self._states):04d}.bin"
        state = _ResourceState(
            key=self._key(message),
            path=path,
            sequence=sequence,
            source=str(message["source"]),
            handle=path.open("wb"),
            digest=hashlib.sha256(),
            page=int(message.get("page", -1)),
        )
        self._states.append(state)
        self._active[state.key] = state
        return state

    def _finish(self, state: _ResourceState | None) -> None:
        if state is None:
            return
        state.handle.flush()
        state.handle.close()
        if self._active.get(state.key) is state:
            self._active.pop(state.key, None)

    def _start(self, message: dict[str, Any]) -> _ResourceState:
        key = self._key(message)
        self._finish(self._active.get(key))
        return self._new_state(message)

    def _run(self) -> None:
        while True:
            item = self._items.get()
            try:
                if item is None:
                    return
                kind, message, payload = item
                if kind == "resource-open":
                    self._start(message)
                elif kind in {"resource-close", "resource-eof"}:
                    self._finish(self._active.get(self._key(message)))
                else:
                    state = self._active.get(self._key(message))
                    if state is None:
                        state = self._new_state(message)
                    if state.size == 0:
                        state.sequence = int(message["sequence"])
                        state.source = str(message["source"])
                        state.page = int(message.get("page", -1))
                    state.handle.write(payload)
                    state.digest.update(payload)
                    state.size += len(payload)
            except Exception as exc:  # report after the callback thread is done
                self._errors.append(str(exc))
            finally:
                self._items.task_done()

    def close(self) -> None:
        self._items.join()
        self._items.put(None)
        self._items.join()
        self._worker.join()
        for state in self._states:
            if not state.handle.closed:
                state.handle.flush()
                state.handle.close()

    def resources(self) -> list[CapturedResource]:
        if self._errors:
            raise RuntimeError("resource writer failed: " + "; ".join(self._errors))
        return [
            CapturedResource(
                path=state.path,
                sequence=state.sequence,
                sha256=state.digest.hexdigest(),
                size=state.size,
                source=state.source,
                page=state.page,
            )
            for state in self._states
            if state.size and state.path.exists()
        ]


def validate_navigation(page_count: int | None, jumps: list[int],
                        captured_pages: int,
                        initial_page: int | None = None) -> None:
    """Reject stale startup state, missing pages, or out-of-order navigation."""
    if page_count is None:
        return
    if initial_page is None:
        raise RuntimeError("viewer did not report its initial page")
    if not 0 <= initial_page < page_count:
        raise RuntimeError(
            f"invalid viewer initial page {initial_page} for {page_count} pages"
        )
    if page_count <= 0:
        raise RuntimeError(f"invalid viewer page count: {page_count}")
    observed: list[int] = []
    seen: set[int] = set()
    for page in jumps:
        if page not in seen:
            seen.add(page)
            observed.append(page)
    expected = list(range(page_count))
    if observed != expected:
        raise RuntimeError(
            f"navigation coverage is not forward-complete: "
            f"observed {len(observed)} unique jumps, expected {page_count}"
        )
    if captured_pages != page_count:
        raise RuntimeError(
            f"captured {captured_pages} unique page resources, "
            f"viewer reported {page_count} logical pages"
        )


def resource_kind(path: Path) -> str | None:
    with path.open("rb") as stream:
        head = stream.read(16)
    if head.startswith(PNG_SIGNATURE):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _valid_image(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, ValueError):
        return False


def _image_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            size = image.size
            image.verify()
        return size
    except (OSError, ValueError):
        return None


def _page_resources(resources: list[CapturedResource]) -> list[CapturedResource]:
    """Keep page-sized resources and discard UI images and thumbnails."""
    images: list[tuple[CapturedResource, tuple[int, int]]] = []
    for resource in sorted(resources, key=lambda item: item.sequence):
        if resource_kind(resource.path) is None:
            continue
        size = _image_size(resource.path)
        if size is not None:
            images.append((resource, size))

    preferred = [item for item in images if item[0].source == PAGE_RESOURCE_SOURCE]
    pool = preferred or images
    if not pool:
        return []

    largest_area = max(width * height for _, (width, height) in pool)
    minimum_area = largest_area * 0.5 if largest_area >= MIN_PAGE_AREA else 0
    return [
        resource for resource, (width, height) in pool
        if width * height >= minimum_area
    ]


def _write_png(resource: CapturedResource, destination: Path) -> bool:
    kind = resource_kind(resource.path)
    if kind is None or not _valid_image(resource.path):
        return False
    if kind == "png":
        shutil.copyfile(resource.path, destination)
        return True
    try:
        with Image.open(resource.path) as image:
            image.load()
            if image.mode == "CMYK":
                image = image.convert("RGB")
            image.save(destination, format="PNG")
        return True
    except (OSError, ValueError):
        destination.unlink(missing_ok=True)
        return False


def export_images(resources: list[CapturedResource], out_dir: Path,
                  initial_page: int | None = None) -> list[Path]:
    """Write logical-page resources in forward order.

    Identical bytes on different logical pages are valid and are retained.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("page_*.png"):
        old.unlink()

    if initial_page is not None and initial_page != 0:
        # The viewer can restore a prior spread before navigation is attached.
        # Those resources have no reliable page association and must not win
        # deduplication over the later page-0 traversal.
        resources = [resource for resource in resources if resource.page >= 0]

    pages: list[Path] = []
    seen: set[tuple[int, str]] = set()
    startup_page = initial_page if initial_page is not None else -1
    for resource in _page_resources(resources):
        page = resource.page
        if page < 0:
            # Startup resources precede the first navigation callback. Treat
            # full-page resources as the restored spread in capture order.
            page = startup_page
            if startup_page >= 0:
                startup_page += 1
        key = (page, resource.sha256)
        if key in seen:
            continue
        destination = out_dir / f"page_{len(pages) + 1:03d}.png"
        if _write_png(resource, destination):
            seen.add(key)
            pages.append(destination)
        else:
            destination.unlink(missing_ok=True)
    return pages


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _association_viewer() -> Path | None:
    """Resolve the executable registered for DMM book files on Windows."""
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None

    for suffix in (".dmme", ".dmmb"):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, suffix) as extension:
                prog_id, _ = winreg.QueryValueEx(extension, None)
            if not isinstance(prog_id, str) or not prog_id:
                continue
            with winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT,
                f"{prog_id}\\shell\\open\\command",
            ) as command_key:
                command, _ = winreg.QueryValueEx(command_key, None)
            parts = shlex.split(str(command), posix=False)
            if not parts:
                continue
            executable = Path(os.path.expandvars(parts[0].strip('"')))
            if executable.name.casefold() != "dmmbookviewer.exe":
                continue
            if not executable.is_absolute():
                located = shutil.which(str(executable))
                if located:
                    executable = Path(located)
            if executable.is_file():
                return executable.resolve()
        except (OSError, TypeError, ValueError):
            continue
    return None


def _standard_viewer_candidates() -> list[Path]:
    candidates: list[Path] = []
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)",
                     "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            candidates.append(
                Path(root) / "DMM" / "DMMbookviewer" / "DMMbookviewer.exe"
            )
    return candidates


def default_viewer() -> Path:
    configured = os.environ.get("DMM_VIEWER")
    if configured:
        return Path(configured).expanduser().resolve()

    associated = _association_viewer()
    if associated:
        return associated

    candidates = _standard_viewer_candidates()
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0] if candidates else Path("DMMbookviewer.exe")


def verify_viewer(path: Path) -> bool:
    if not path.is_file():
        raise RuntimeError(f"viewer not found: {path}; set DMM_VIEWER to DMMbookviewer.exe")
    actual = _sha256(path)
    if actual != VIEWER_SHA256:
        print(
            f"[viewer-hash] unsupported {actual}; trying signature fallback",
            flush=True,
        )
        return False
    return True


def stop_viewer() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", "DMMbookviewer.exe"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def find_viewer_pid(device: Any, timeout: float = 20.0) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for process in device.enumerate_processes():
            if process.name.lower() == "dmmbookviewer.exe":
                return process.pid
        time.sleep(0.05)
    return None


def _script_for(session: Any, mode: str, writer: ResourceWriter,
                traverse: bool, wait_ms: int,
                load_job_rva: int | None) -> tuple[Any, NavigationState]:
    navigation = NavigationState(
        done=threading.Event(),
        load_job_matches=1 if load_job_rva is not None else None,
    )
    if not traverse:
        navigation.done.set()

    def on_message(message: dict[str, Any], data: Any) -> None:
        if message.get("type") == "error":
            print("[js-error]", message.get("stack") or message.get("description"), flush=True)
            return
        payload = message.get("payload")
        if not isinstance(payload, dict):
            if payload is not None:
                print(payload, flush=True)
            return
        kind = payload.get("type")
        if kind == "resource-chunk":
            if data is not None:
                writer.submit(payload, data)
        elif kind in {"resource-open", "resource-close", "resource-eof"}:
            writer.submit_boundary(kind, payload)
        elif kind == "load-job-resolved":
            try:
                navigation.load_job_matches = int(payload["matches"])
            except (KeyError, TypeError, ValueError):
                navigation.load_job_matches = 0
            print(f"[{kind}] {payload}", flush=True)
        elif kind == "navigation-properties":
            try:
                navigation.page_count = int(payload["pageCount"])
                if navigation.initial_page is None:
                    navigation.initial_page = int(payload["currentPage"])
            except (KeyError, TypeError, ValueError):
                pass
            print(f"[{kind}] {payload}", flush=True)
        elif kind == "navigation-jump":
            try:
                navigation.jumps.append(int(payload["page"]))
            except (KeyError, TypeError, ValueError):
                pass
            print(f"[{kind}] {payload}", flush=True)
        elif kind == "navigation-done":
            if navigation.page_count is None:
                try:
                    navigation.page_count = int(payload["pageCount"])
                except (KeyError, TypeError, ValueError):
                    pass
            navigation.done.set()
            print(f"[{kind}] {payload}", flush=True)
        elif kind in {
            "hook-installed", "hook-missing", "hook-error", "capture-ready",
            "hooks-ready", "qt-hooks-ready", "navigation-ready", "navigation-missing",
            "navigation-error", "navigation-canvas", "navigation-retry",
        }:
            if kind in {"hook-installed", "hook-missing", "hook-error"} and \
                    payload.get("name") == "load_job.ReadRawData":
                navigation.load_job_hooked = kind == "hook-installed"
            print(f"[{kind}] {payload}", flush=True)

    source = (
        JS.replace("__MODE__", mode)
        .replace("__LOAD_JOB_RVA__", "null" if load_job_rva is None else hex(load_job_rva))
        .replace("__LOAD_JOB_SIGNATURE__", LOAD_JOB_READ_RAW_SIGNATURE)
        .replace("__TRAVERSE__", "true" if traverse else "false")
        .replace("__NAV_WAIT_MS__", str(wait_ms))
    )
    script = session.create_script(source)
    script.on("message", on_message)
    script.load()
    return script, navigation


def start_capture(device: Any, viewer: Path, book: Path,
                  writer: ResourceWriter, traverse: bool,
                  wait_ms: int, load_job_rva: int | None) -> tuple[Any, int, NavigationState]:
    """Attach before resume; association fallback is only for spawn failures."""
    suspended_pid = None
    try:
        suspended_pid = device.spawn([str(viewer), str(book)])
        session = device.attach(suspended_pid)
        script, navigation = _script_for(
            session, book.suffix.lower().lstrip("."), writer,
            traverse, wait_ms, load_job_rva,
        )
        device.resume(suspended_pid)
        return session, suspended_pid, navigation
    except Exception as spawn_error:
        if suspended_pid is not None:
            try:
                device.kill(suspended_pid)
            except Exception:
                pass
        print(f"[spawn-fallback] {spawn_error}", flush=True)

    os.startfile(str(book))
    pid = find_viewer_pid(device)
    if pid is None:
        raise RuntimeError("viewer did not start through the file association")
    session = device.attach(pid)
    script, navigation = _script_for(
        session, book.suffix.lower().lstrip("."), writer,
        traverse, wait_ms, load_job_rva,
    )
    return session, pid, navigation


def wait_for_resources(writer: ResourceWriter, settle_seconds: float,
                       timeout_seconds: float, navigation: NavigationState) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if navigation.load_job_matches is not None and navigation.load_job_matches != 1:
            raise RuntimeError(
                "load_job.ReadRawData signature did not resolve uniquely: "
                f"{navigation.load_job_matches} matches"
            )
        if navigation.load_job_hooked is False:
            raise RuntimeError("load_job.ReadRawData hook was not installed")

        if navigation.done.is_set() and writer.chunks and \
                time.monotonic() - writer.last_message >= settle_seconds:
            return
        time.sleep(0.25)
    if not navigation.done.is_set():
        raise RuntimeError("page navigation did not complete before timeout")
    if not writer.chunks:
        raise RuntimeError(
            "no resource reads observed; the installed hooks are not active "
            "for this viewer path (temporary resources were kept for diagnosis)"
        )
    print("[timeout] stopping after resource activity timeout", flush=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("book", type=Path)
    parser.add_argument("out_dir", type=Path, nargs="?")
    parser.add_argument("--viewer", type=Path, default=None)
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    parser.add_argument("--navigation-wait-ms", type=int, default=0)
    parser.add_argument("--no-traverse", action="store_true")
    parser.add_argument("--keep-resources", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if os.name != "nt":
        print("DMMbookviewer capture requires Windows", file=sys.stderr)
        return 2

    book = args.book.expanduser().resolve()
    if book.suffix.lower() not in {".dmme", ".dmmb"}:
        print("book must end in .dmme or .dmmb", file=sys.stderr)
        return 2
    if not book.is_file():
        print(f"book not found: {book}", file=sys.stderr)
        return 2

    viewer = (args.viewer or default_viewer()).expanduser().resolve()
    try:
        load_job_rva = LOAD_JOB_READ_RAW_RVA if verify_viewer(viewer) else None
    except RuntimeError as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 2

    out_dir = (args.out_dir or Path("dump") / book.stem).expanduser().resolve()
    resources_dir = out_dir / "_resources"
    if resources_dir.exists():
        shutil.rmtree(resources_dir)
    resources_dir.mkdir(parents=True, exist_ok=True)
    writer = ResourceWriter(resources_dir)
    session = None
    pid = None
    device = None
    try:
        stop_viewer()
        try:
            import frida
        except ImportError as exc:
            raise RuntimeError("install frida-tools in the active uv environment") from exc

        device = frida.get_local_device()
        print(f"[open] {book}", flush=True)
        session, pid, navigation = start_capture(
            device, viewer, book, writer, not args.no_traverse,
            args.navigation_wait_ms, load_job_rva,
        )
        print(f"[pid] {pid}", flush=True)
        wait_for_resources(writer, args.settle_seconds, args.timeout_seconds,
                           navigation)
    except Exception as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 1
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
        writer.close()
        if pid is not None and device is not None:
            try:
                device.kill(pid)
            except Exception:
                pass

    try:
        resources = writer.resources()
        pages = export_images(resources, out_dir, navigation.initial_page)
        validate_navigation(
            navigation.page_count,
            navigation.jumps,
            len(pages),
            navigation.initial_page,
        )
    except (OSError, RuntimeError) as exc:
        for page in out_dir.glob("page_*.png"):
            page.unlink(missing_ok=True)
        print(f"[fail] {exc}", file=sys.stderr)
        return 1
    if not pages:
        print(f"[fail] no image resources found; inspect {resources_dir}", file=sys.stderr)
        return 1
    if not args.keep_resources:
        shutil.rmtree(resources_dir, ignore_errors=True)
    print(f"[done] {len(pages)} pages -> {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
