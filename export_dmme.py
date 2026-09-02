#!/usr/bin/env python3
"""Capture decrypted book resources from the installed DMMbookviewer."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import posixpath
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from PIL import Image

VIEWER_SHA256 = "edfac9ac051fdb6726dcc77168d661f546c062e64b3e05af405f2b2bf71cfd5f"
LOAD_JOB_READ_RAW_RVA = 0x8B340
# The tail-jump displacement is link-dependent; the invariant prefix ends at E9.
LOAD_JOB_READ_RAW_SIGNATURE = "45 89 01 48 8B 89 18 01 00 00 4D 8B C1 E9"
EPUB_SUFFIXES = {".dmme", ".dmmr"}
SUPPORTED_SUFFIXES = EPUB_SUFFIXES | {".dmmb"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PAGE_RESOURCE_SOURCE = "qt.QPixmap.loadFromData"
MIN_PAGE_AREA = 500_000
IMAGE_EXTENSIONS = {
    "jpeg": "jpg",
    "png": "png",
    "gif": "gif",
    "webp": "webp",
    "bmp": "bmp",
    "tiff": "tiff",
    "ico": "ico",
    "jp2": "jp2",
    "j2k": "j2k",
    "jpx": "jpx",
    "tga": "tga",
    "dds": "dds",
    "ppm": "ppm",
    "pgm": "pgm",
    "pbm": "pbm",
    "pcx": "pcx",
    "xbm": "xbm",
    "avif": "avif",
}

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
    return installURLRead('load_job.ReadRawData', target);
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
    return installURLRead('load_job.ReadRawData', matches[0]);
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
      onLeave(_) {
        try {
          if (!this.bytesRead || this.bytesRead.isNull()) return;
          const n = this.bytesRead.readS32();
          if (n > 0 && n <= MAX_CHUNK && this.buffer && !this.buffer.isNull()) {
            const data = this.buffer.add(16).readPointer();
            if (data && !data.isNull()) {
              send({
                type: 'resource-chunk',
                source: name,
                owner: this.owner,
                url: urlForOwner(this.owner),
                sequence: this.sequence,
                requested: this.capacity,
                size: n
              }, data.readByteArray(n));
            }
          }
          if (n <= 0) {
            send({
              type: 'resource-eof',
              source: name,
              owner: this.owner,
              url: urlForOwner(this.owner),
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

const requestUrls = new Map();
const jobUrls = new Map();
let urlMetadataInstalled = false;

function readStdString(object) {
  try {
    if (!object || object.isNull()) return '';
    const size = object.add(16).readU64().toNumber();
    const capacity = object.add(24).readU64().toNumber();
    if (size > 16384 || capacity > 0x1000000) return '';
    const data = capacity <= 15 ? object : object.readPointer();
    if (!data || data.isNull()) return '';
    return data.readUtf8String(size) || '';
  } catch (_) { return ''; }
}

function readGURLSpec(gurl) {
  try {
    const module = Process.findModuleByName('url_lib.dll');
    const address = module && module.findExportByName(
      '?spec@GURL@@QEBAAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@XZ'
    );
    if (!address) return '';
    const spec = new NativeFunction(address, 'pointer', ['pointer'])(gurl);
    return readStdString(spec);
  } catch (_) { return ''; }
}

function urlForOwner(owner) {
  return requestUrls.get(owner) || jobUrls.get(owner) || '';
}

function installURLMetadata() {
  if (urlMetadataInstalled) return true;
  const requestCtor = findModuleExport('net.dll',
    '??0URLRequest@net@@QEAA@AEBVGURL@@W4RequestPriority@1@PEAVDelegate@01@PEBVURLRequestContext@1@@Z'
  );
  const jobCtor = findModuleExport('net.dll',
    '??0URLRequestJob@net@@QEAA@PEAVURLRequest@1@PEAVNetworkDelegate@1@@Z'
  );
  if (requestCtor) {
    Interceptor.attach(requestCtor, {
      onEnter(args) {
        const owner = args[0].toString();
        const url = readGURLSpec(args[1]);
        requestUrls.set(owner, url);
      }
    });
  }
  if (jobCtor) {
    Interceptor.attach(jobCtor, {
      onEnter(args) {
        const owner = args[0].toString();
        const request = args[1].toString();
        jobUrls.set(owner, requestUrls.get(request) || '');
      }
    });
  }
  urlMetadataInstalled = !!requestCtor || !!jobCtor;
  send({type: 'url-metadata-ready', requestCtor: !!requestCtor,
        jobCtor: !!jobCtor});
  return urlMetadataInstalled;
}

function installNetHooks() {
  if (!Process.findModuleByName('net.dll')) return false;
  installURLMetadata();
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
    url: str = ""


@dataclass
class _ResourceState:
    key: str
    path: Path
    sequence: int
    source: str
    handle: Any
    digest: Any
    page: int = -1
    url: str = ""
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
            url=str(message.get("url", "")),
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
                    if state is not None and message.get("url") and state.url and \
                            str(message["url"]) != state.url:
                        self._finish(state)
                        state = None
                    if state is None:
                        state = self._new_state(message)
                    if state.size == 0:
                        state.sequence = int(message["sequence"])
                        state.source = str(message["source"])
                        state.page = int(message.get("page", -1))
                    if message.get("url"):
                        state.url = str(message["url"])
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
                url=state.url,
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
    try:
        with Image.open(path) as image:
            kind = (image.format or "").casefold()
        return kind if kind in IMAGE_EXTENSIONS else None
    except (OSError, ValueError):
        return None


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


def _copy_image(resource: CapturedResource, destination: Path) -> bool:
    if resource_kind(resource.path) is None:
        return False
    try:
        shutil.copyfile(resource.path, destination)
        return True
    except OSError:
        destination.unlink(missing_ok=True)
        return False


def export_images(resources: list[CapturedResource], out_dir: Path,
                  initial_page: int | None = None) -> list[Path]:
    """Write logical-page resources in forward order, preserving image bytes."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("page_*"):
        if old.is_file():
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
        kind = resource_kind(resource.path)
        if kind is None:
            continue
        destination = out_dir / (
            f"page_{len(pages) + 1:03d}.{IMAGE_EXTENSIONS[kind]}"
        )
        if _copy_image(resource, destination):
            seen.add(key)
            pages.append(destination)
        else:
            destination.unlink(missing_ok=True)
    return pages


def _epub_relative_path(url: str) -> str | None:
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        return None
    prefix = "/item/"
    if not path.startswith(prefix):
        return None
    relative = posixpath.normpath(path[len(prefix):])
    if (not relative or relative in {".", ".."} or relative.startswith("../")
            or relative.startswith("/") or "\\" in relative):
        return None
    return relative


def _epub_mime(relative: str) -> str:
    suffix = PurePosixPath(relative).suffix.casefold()
    return {
        ".xhtml": "application/xhtml+xml",
        ".html": "application/xhtml+xml",
        ".css": "text/css",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".jp2": "image/jp2",
        ".j2k": "image/jp2",
        ".jpx": "image/jp2",
        ".avif": "image/avif",
        ".svg": "image/svg+xml",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".js": "text/javascript",
        ".xml": "application/xml",
    }.get(suffix, "application/octet-stream")


def _epub_resource_rank(resource: CapturedResource) -> tuple[int, int, int]:
    source_rank = {
        "load_job.ReadRawData": 0,
        "net.URLRequest.Read": 1,
    }.get(resource.source, 2)
    return source_rank, -resource.size, resource.sequence


def _epub_resources(resources: list[CapturedResource]) -> dict[str, CapturedResource]:
    selected: dict[str, CapturedResource] = {}
    for resource in sorted(resources, key=lambda item: item.sequence):
        if not resource.url or not resource.path.exists():
            continue
        relative = _epub_relative_path(resource.url)
        if relative is None:
            continue
        current = selected.get(relative)
        if current is None or _epub_resource_rank(resource) < _epub_resource_rank(current):
            selected[relative] = resource
    return selected


def _epub_title(resources: dict[str, CapturedResource], documents: list[str]) -> str:
    for relative in documents:
        try:
            root = ET.fromstring(resources[relative].path.read_bytes())
        except (ET.ParseError, OSError):
            continue
        for element in root.iter():
            if isinstance(element.tag, str) and element.tag.rsplit("}", 1)[-1] == "title":
                title = " ".join("".join(element.itertext()).split())
                if title:
                    return title
    return "DMM book"


def _epub_nav(title: str, documents: list[str]) -> bytes:
    xhtml = "http://www.w3.org/1999/xhtml"
    epub = "http://www.idpf.org/2007/ops"
    ET.register_namespace("", xhtml)
    ET.register_namespace("epub", epub)
    root = ET.Element(f"{{{xhtml}}}html", {"{http://www.w3.org/XML/1998/namespace}lang": "ja"})
    head = ET.SubElement(root, f"{{{xhtml}}}head")
    ET.SubElement(head, f"{{{xhtml}}}title").text = title
    body = ET.SubElement(root, f"{{{xhtml}}}body")
    nav = ET.SubElement(body, f"{{{xhtml}}}nav", {f"{{{epub}}}type": "toc"})
    ET.SubElement(nav, f"{{{xhtml}}}h1").text = title
    ordered = ET.SubElement(nav, f"{{{xhtml}}}ol")
    for index, relative in enumerate(documents, 1):
        item = ET.SubElement(ordered, f"{{{xhtml}}}li")
        ET.SubElement(item, f"{{{xhtml}}}a", {"href": relative}).text = str(index)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def export_epub(resources: list[CapturedResource], destination: Path,
                book: Path, fixed_layout: bool = False) -> Path:
    """Rebuild the EPUB resources exposed by the Reader's resource protocol."""
    selected = _epub_resources(resources)
    documents = [
        relative for relative in selected
        if PurePosixPath(relative).suffix.casefold() in {".xhtml", ".html"}
    ]
    if not documents:
        raise RuntimeError("no EPUB document resources captured; keep resources for diagnosis")

    opf_ns = "http://www.idpf.org/2007/opf"
    dc_ns = "http://purl.org/dc/elements/1.1/"
    container_ns = "urn:oasis:names:tc:opendocument:xmlns:container"
    ET.register_namespace("", opf_ns)
    ET.register_namespace("dc", dc_ns)
    ET.register_namespace("dcterms", "http://purl.org/dc/terms/")
    package_attributes = {"version": "3.0", "unique-identifier": "pub-id"}
    if fixed_layout:
        package_attributes["prefix"] = (
            "rendition: http://www.idpf.org/vocab/rendition/#"
        )
    package = ET.Element(f"{{{opf_ns}}}package", package_attributes)
    metadata = ET.SubElement(package, f"{{{opf_ns}}}metadata")
    title = _epub_title(selected, documents)
    ET.SubElement(metadata, f"{{{dc_ns}}}title").text = title
    ET.SubElement(metadata, f"{{{dc_ns}}}language").text = "ja"
    identifier = ET.SubElement(metadata, f"{{{dc_ns}}}identifier", {"id": "pub-id"})
    identifier.text = book.stem
    ET.SubElement(
        metadata,
        f"{{{opf_ns}}}meta",
        {"property": "dcterms:modified"},
    ).text = datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    if fixed_layout:
        ET.SubElement(
            metadata,
            f"{{{opf_ns}}}meta",
            {"property": "rendition:layout"},
        ).text = "pre-paginated"
    manifest = ET.SubElement(package, f"{{{opf_ns}}}manifest")
    ids: dict[str, str] = {}
    for index, relative in enumerate(selected, 1):
        item_id = f"item-{index}"
        ids[relative] = item_id
        properties = ""
        if PurePosixPath(relative).name.casefold() in {"cover.jpg", "cover.jpeg", "cover.png"}:
            properties = "cover-image"
        attributes = {
            "id": item_id,
            "href": relative,
            "media-type": _epub_mime(relative),
        }
        if properties:
            attributes["properties"] = properties
        ET.SubElement(manifest, f"{{{opf_ns}}}item", attributes)
    ET.SubElement(
        manifest,
        f"{{{opf_ns}}}item",
        {"id": "nav", "href": "nav.xhtml", "media-type": "application/xhtml+xml",
         "properties": "nav"},
    )
    spine = ET.SubElement(package, f"{{{opf_ns}}}spine", {"page-progression-direction": "rtl"})
    for relative in documents:
        ET.SubElement(spine, f"{{{opf_ns}}}itemref", {"idref": ids[relative]})

    container = ET.Element(f"{{{container_ns}}}container", {"version": "1.0"})
    rootfiles = ET.SubElement(container, f"{{{container_ns}}}rootfiles")
    ET.SubElement(
        rootfiles,
        f"{{{container_ns}}}rootfile",
        {"full-path": "OEBPS/content.opf", "media-type": "application/oebps-package+xml"},
    )
    ET.register_namespace("", container_ns)
    container_bytes = ET.tostring(container, encoding="utf-8", xml_declaration=True)
    opf_bytes = ET.tostring(package, encoding="utf-8", xml_declaration=True)
    nav_bytes = _epub_nav(title, documents)

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(destination) + ".part")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml", container_bytes, zipfile.ZIP_DEFLATED)
            archive.writestr("OEBPS/content.opf", opf_bytes, zipfile.ZIP_DEFLATED)
            archive.writestr("OEBPS/nav.xhtml", nav_bytes, zipfile.ZIP_DEFLATED)
            for relative, resource in selected.items():
                archive.write(resource.path, f"OEBPS/{relative}", zipfile.ZIP_DEFLATED)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def export_fixed_epub(resources: list[CapturedResource], destination: Path,
                       book: Path, page_count: int | None = None) -> Path:
    """Package fixed-layout page images as a standard EPUB publication."""
    candidates = [
        resource for resource in _page_resources(resources)
        if resource_kind(resource.path) is not None
    ]
    if page_count is not None and page_count > 0:
        if len(candidates) == page_count + 1:
            # The restored page is painted before the first controlled jump.
            candidates = candidates[1:]
        if len(candidates) != page_count:
            raise RuntimeError(
                f"captured {len(candidates)} fixed-layout pages, "
                f"viewer reported {page_count}"
            )
        candidates = sorted(candidates, key=lambda resource: resource.sequence)
    if not candidates:
        raise RuntimeError("no fixed-layout page images captured; keep resources for diagnosis")

    extensions = IMAGE_EXTENSIONS
    xhtml_ns = "http://www.w3.org/1999/xhtml"
    ET.register_namespace("", xhtml_ns)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        synthetic: list[CapturedResource] = []
        for index, resource in enumerate(candidates, 1):
            kind = resource_kind(resource.path)
            if kind not in extensions:
                continue
            extension = extensions[kind]
            image_relative = f"image/page-{index:04d}.{extension}"
            document_relative = f"xhtml/page-{index:04d}.xhtml"
            document_path = root / f"page-{index:04d}.xhtml"
            document = ET.Element(f"{{{xhtml_ns}}}html")
            head = ET.SubElement(document, f"{{{xhtml_ns}}}head")
            ET.SubElement(head, f"{{{xhtml_ns}}}title").text = book.stem
            width, height = _image_size(resource.path) or (1, 1)
            ET.SubElement(head, f"{{{xhtml_ns}}}meta", {"charset": "UTF-8"})
            ET.SubElement(
                head,
                f"{{{xhtml_ns}}}meta",
                {"name": "viewport", "content": f"width={width}, height={height}"},
            )
            body = ET.SubElement(
                document,
                f"{{{xhtml_ns}}}body",
                {"style": "margin:0; padding:0;"},
            )
            ET.SubElement(
                body,
                f"{{{xhtml_ns}}}img",
                {
                    "src": f"../{image_relative}",
                    "alt": "",
                    "style": "width:100%; height:100%; object-fit:contain;",
                },
            )
            document_path.write_bytes(
                ET.tostring(document, encoding="utf-8", xml_declaration=True)
            )
            synthetic.append(
                CapturedResource(
                    path=document_path,
                    sequence=index * 2 - 2,
                    sha256=_sha256(document_path),
                    size=document_path.stat().st_size,
                    source="load_job.ReadRawData",
                    url=f"cjh://fixed/item/{document_relative}",
                )
            )
            synthetic.append(
                CapturedResource(
                    path=resource.path,
                    sequence=index * 2 - 1,
                    sha256=resource.sha256,
                    size=resource.size,
                    source=resource.source,
                    url=f"cjh://fixed/item/{image_relative}",
                )
            )
        return export_epub(synthetic, destination, book, fixed_layout=True)


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

    for suffix in (".dmme", ".dmmb", ".dmmr"):
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
            "url-metadata-ready", "url-request", "url-job",
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
    suffix = book.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        print("book must end in .dmmb, .dmme, or .dmmr", file=sys.stderr)
        return 2
    epub_mode = suffix in EPUB_SUFFIXES
    traverse = suffix in {".dmmb", ".dmme"} and not args.no_traverse
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
            device, viewer, book, writer, traverse,
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
        if epub_mode:
            if suffix == ".dmme" and not _epub_resources(resources):
                output = export_fixed_epub(
                    resources, out_dir / f"{book.stem}.epub", book,
                    navigation.page_count,
                )
            else:
                output = export_epub(resources, out_dir / f"{book.stem}.epub", book)
        else:
            pages = export_images(resources, out_dir, navigation.initial_page)
            validate_navigation(
                navigation.page_count,
                navigation.jumps,
                len(pages),
                navigation.initial_page,
            )
    except (OSError, RuntimeError) as exc:
        for page in out_dir.glob("page_*"):
            if page.is_file():
                page.unlink(missing_ok=True)
        print(f"[fail] {exc}", file=sys.stderr)
        return 1
    if epub_mode:
        if not args.keep_resources:
            shutil.rmtree(resources_dir, ignore_errors=True)
        print(f"[done] {output}", flush=True)
        return 0
    if not pages:
        print(f"[fail] no image resources found; inspect {resources_dir}", file=sys.stderr)
        return 1
    if not args.keep_resources:
        shutil.rmtree(resources_dir, ignore_errors=True)
    print(f"[done] {len(pages)} pages -> {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
