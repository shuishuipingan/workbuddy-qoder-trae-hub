"""Extract text only; called in a bounded subprocess, with no document rendering."""
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
import zipfile

MAX_TEXT = 256 * 1024
MAX_INPUT = 8 * 1024 * 1024
MAX_XML = 2 * 1024 * 1024
MAX_ALL_XML = 8 * 1024 * 1024
MAX_PAGES = 40
PDF_WHEEL = 'pypdf-7.1.1-py3-none-any.whl'
_SLOTS = threading.BoundedSemaphore(2)


class ExtractionError(ValueError):
    pass


def kind_for(name, mime, data):
    suffix = name.rsplit('.', 1)[-1].lower()
    if data[:5] == b'%PDF-' or mime == 'application/pdf' or suffix == 'pdf':
        return 'pdf'
    if suffix in ('docx', 'xlsx', 'pptx'):
        return suffix
    return {
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
    }.get(mime)


def extract(kind, data):
    if not _SLOTS.acquire(blocking=False):
        raise ExtractionError('document extraction is busy; retry later')
    try:
        exe = sys.executable
        if Path(exe).name.lower().startswith('pythonw'):
            exe = str(Path(exe).with_name(Path(exe).name.replace('pythonw', 'python', 1)))
        result = subprocess.run(
            [exe, '-I', str(Path(__file__).resolve()), kind], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if result.returncode or len(result.stdout) > MAX_TEXT * 6 + 2048:
            raise ExtractionError('invalid document or extraction did not complete')
        reply = json.loads(result.stdout.decode('utf-8'))
        if reply.get('error'):
            raise ExtractionError(reply['error'])
        return reply['text']
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError('document extraction exceeded its 10-second limit') from exc
    except ExtractionError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ExtractionError('document extractor is not available') from exc
    finally:
        _SLOTS.release()


class Text:
    def __init__(self):
        self.parts = []
        self.size = 0
        self.truncated = False

    def add(self, text):
        if self.truncated or not text:
            return
        encoded = text.encode('utf-8', 'replace')
        space = MAX_TEXT - 128 - self.size
        if len(encoded) > space:
            encoded = encoded[:space]
            self.truncated = True
        self.size += len(encoded)
        self.parts.append(encoded.decode('utf-8', 'ignore'))

    def finish(self):
        text = ''.join(self.parts).strip()
        if not text:
            raise ExtractionError('no extractable text; scanned PDFs need an OCR or text export first')
        if self.truncated:
            text += '\n[Attachment excerpt truncated by gateway]'
        return text


def _pdf(data, out):
    if not data.startswith(b'%PDF-'):
        raise ExtractionError('invalid PDF header')
    if sys.version_info < (3, 10):
        raise ExtractionError('PDF extraction needs Python 3.10 or newer')
    wheel = Path(__file__).resolve().parent / 'vendor' / PDF_WHEEL
    if not wheel.is_file():
        raise ExtractionError('local PDF extraction dependency is missing')
    sys.path.insert(0, str(wheel))
    # Third-party package is optional and loaded only from the bundled wheel.
    PdfReader = __import__("pypdf").PdfReader
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        raise ExtractionError('password-protected PDF; provide an unlocked copy')
    pages = len(reader.pages)
    found = False
    for i in range(min(MAX_PAGES, pages)):
        text = reader.pages[i].extract_text() or ''
        if text.strip():
            found = True
            out.add('[Page %d]\n%s\n' % (i + 1, text))
        if out.truncated:
            break
    if not found:
        raise ExtractionError('no extractable text on the first 40 PDF pages; scanned PDFs need OCR first')
    if pages > MAX_PAGES:
        out.add('\n[Only the first 40 pages were examined]')


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def _strings(root, kinds):
    return ''.join(node.text or '' for node in root.iter() if _local(node.tag) in kinds)


def _office(kind, data, out):
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entries = z.infolist()
        sizes = {entry.filename: entry.file_size for entry in entries}
        if len(entries) > 2048 or len(sizes) != len(entries) or '[Content_Types].xml' not in sizes:
            raise ExtractionError('invalid or oversized Office archive')

        def root_of(name):
            nonlocal total
            size = sizes[name]
            if size > MAX_XML or total + size > MAX_ALL_XML:
                raise ExtractionError('Office XML exceeds the extraction limit')
            total += size
            with z.open(name) as fp:
                raw = fp.read(MAX_XML + 1)
            if len(raw) != size:
                raise ExtractionError('invalid Office XML size')
            text = raw.decode('utf-8-sig')
            if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I):
                raise ExtractionError('Office XML document type declarations are not supported')
            return ET.fromstring(text)

        def numbered(pattern):
            matches = [(int(m[1]), name) for name in sizes if (m := re.fullmatch(pattern, name))]
            return [name for _, name in sorted(matches)]

        def paragraphs(root):
            for node in root.iter():
                if _local(node.tag) == 'p':
                    text = _strings(node, ('t',))
                    if text:
                        out.add(text + '\n')
                if out.truncated:
                    break

        if kind == 'docx':
            paragraphs(root_of('word/document.xml'))
        elif kind == 'pptx':
            slides = numbered(r'ppt/slides/slide([0-9]{1,6})\.xml')
            for i, name in enumerate(slides[:MAX_PAGES], 1):
                root = root_of(name)
                if _strings(root, ('t',)).strip():
                    out.add('[Slide %d]\n' % i)
                    paragraphs(root)
                if out.truncated:
                    break
            if len(slides) > MAX_PAGES and out.parts:
                out.add('\n[Only the first 40 slides were examined]')
        else:
            sheets = numbered(r'xl/worksheets/sheet([0-9]{1,6})\.xml')
            shared = []
            if 'xl/sharedStrings.xml' in sizes:
                root = root_of('xl/sharedStrings.xml')
                shared = [_strings(n, ('t',)) for n in root if _local(n.tag) == 'si']
            for i, name in enumerate(sheets[:MAX_PAGES], 1):
                root = root_of(name)
                labelled = False
                for cell in (x for x in root.iter() if _local(x.tag) == 'c'):
                    typ = cell.get('t')
                    val = _strings(cell, ('t',)) if typ == 'inlineStr' else _strings(cell, ('v',))
                    if typ == 's':
                        val = shared[int(val)] if re.fullmatch(r'[0-9]{1,8}', val) and int(val) < len(shared) else ''
                    if val:
                        if not labelled:
                            out.add('[Sheet %d]\n' % i)
                            labelled = True
                        out.add('%s: %s\n' % (cell.get('r', ''), val))
                    if out.truncated:
                        break
                if out.truncated:
                    break
            if len(sheets) > MAX_PAGES and out.parts:
                out.add('\n[Only the first 40 worksheets were examined]')


def _worker():
    try:
        data = sys.stdin.buffer.read(MAX_INPUT + 1)
        if not data or len(data) > MAX_INPUT:
            raise ExtractionError('document exceeds byte limit or is empty')
        out = Text()
        if sys.argv[1] == 'pdf':
            _pdf(data, out)
        elif sys.argv[1] in ('docx', 'pptx', 'xlsx'):
            _office(sys.argv[1], data, out)
        else:
            raise ExtractionError('unsupported document type')
        reply = {'text': out.finish()}
    except ExtractionError as exc:
        reply = {'error': str(exc)}
    except Exception:
        reply = {'error': 'invalid, encrypted or unsupported document structure'}
    sys.stdout.buffer.write(json.dumps(reply, ensure_ascii=True).encode('ascii'))


if __name__ == '__main__':
    _worker()
