#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small, dependency-free MCP server and CLI for Айрен ``.itx`` tests.

The server deliberately uses the MCP JSON-RPC stdio transport directly.  This
keeps the tool usable on a clean Python installation; no third-party package
is required.  It implements creation, safe inspection/unpacking, validation,
preview, and optional local opening through the Ayren file association.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import stat
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote
import xml.etree.ElementTree as ET


ITX_VERSION = "6"
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-06-18"}
QUESTION_TYPES = {"select", "input", "match", "order", "classify"}
__version__ = "0.1.4"
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_MEMBER_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 2000
IMAGE_EXTENSIONS = {".png", ".jpg"}
DATA_EXTENSION = ".dat"
MAX_IMAGE_PIXELS = 20_000_000
PLACEHOLDER_RE = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def levenshtein(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    location: str = ""

    def as_dict(self) -> Dict[str, str]:
        d = {"severity": self.severity, "code": self.code, "message": self.message}
        if self.location:
            d["location"] = self.location
        return d


class ItxError(Exception):
    pass


def finding(severity: str, code: str, message: str, location: str = "") -> Finding:
    return Finding(severity, code, message, location)


def as_bool(value: Optional[str], default: Optional[bool] = None) -> Optional[bool]:
    if value is None:
        return default
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return None


def content_text(node: Optional[ET.Element]) -> str:
    """Return readable text while retaining inline formula/image markers."""
    if node is None:
        return ""
    parts: List[str] = []
    for child in node.iter():
        if child.tag == "text":
            parts.append(child.get("value", ""))
        elif child.tag == "formula":
            parts.append(child.get("source", ""))
        elif child.tag == "img":
            parts.append("[image: %s]" % child.get("src", ""))
        elif child.tag == "br":
            parts.append("\n")
    return "".join(parts)


def content_html(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    parts: List[str] = []
    for child in node.iter():
        if child.tag == "text":
            parts.append(html.escape(child.get("value", "")))
        elif child.tag == "formula":
            source = child.get("source", "")
            parts.append('<span class="formula">%s</span>' % html.escape(source))
        elif child.tag == "img":
            src = child.get("src", "")
            safe = safe_image_path(src) or ""
            if safe:
                parts.append('<img src="%s" alt="" loading="lazy">' % quote(safe, safe="/"))
        elif child.tag == "br":
            parts.append("<br>")
    return "".join(parts)


def safe_archive_path(value: str) -> Optional[str]:
    # Reject Windows drive/ADS/device paths even when running on Linux.
    if not value or "\\" in value or any(ord(c) < 32 or c in ':<>"|?*' for c in value):
        return None
    parts = value.split("/")
    if any(p in {"", ".", ".."} or p.endswith((" ", ".")) for p in parts):
        return None
    if any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])", p.split(".")[0]) for p in parts):
        return None
    return value


def safe_image_path(value: str) -> Optional[str]:
    safe = safe_archive_path(value)
    return safe if safe and safe.startswith("images/") and PurePosixPath(safe).suffix in IMAGE_EXTENSIONS else None


def safe_data_path(value: str) -> Optional[str]:
    """Return a safe Ayren clipboard/source-data member path."""
    safe = safe_archive_path(value)
    return safe if safe and safe.startswith("images/") and PurePosixPath(safe).suffix.lower() == DATA_EXTENSION else None


def checked_write_path(path: Path, overwrite: bool = False) -> None:
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ItxError("Запись через символическую ссылку запрещена: %s" % path)
    if path.exists() and not overwrite:
        raise ItxError("Путь уже существует; выберите новый путь или overwrite=true: %s" % path)


def absolute_path(value: str) -> Path:
    # Keep symlinks unresolved so checked_write_path can detect them.
    return Path(os.path.abspath(os.path.expanduser(value)))


def read_itx(source: str) -> Tuple[Path, bytes, Dict[str, bytes]]:
    path = Path(source).expanduser().resolve()
    if path.is_dir():
        xml_path = path / "test.xml"
        if not xml_path.is_file() or xml_path.is_symlink():
            raise ItxError("Каталог не содержит test.xml: %s" % path)
        if xml_path.stat().st_size > MAX_XML_BYTES:
            raise ItxError("test.xml превышает лимит размера")
        files: Dict[str, bytes] = {"test.xml": xml_path.read_bytes()}
        # A directory may also contain private files. Only referenced raster
        # images are collected, never arbitrary neighbouring documents/secrets.
        root = parse_xml(files["test.xml"])
        referenced = []
        for img in root.findall(".//img"):
            name = safe_image_path(img.get("src", ""))
            if not name:
                raise ItxError("Недопустимый путь изображения")
            referenced.append(name)
            clipboard = img.get("clipboardData")
            if clipboard is not None:
                data_name = safe_data_path(clipboard)
                if not data_name:
                    raise ItxError("Недопустимый путь исходных данных изображения")
                referenced.append(data_name)
        for name in referenced:
            f = path / name
            if not f.exists():
                continue  # validator reports missing references
            if f.is_symlink() or not f.is_file() or path not in f.resolve().parents:
                raise ItxError("Изображение выходит из каталога теста")
            if f.stat().st_size > MAX_MEMBER_BYTES:
                raise ItxError("Изображение превышает лимит размера")
            files[name] = f.read_bytes()
            if len(files) > MAX_MEMBERS or sum(map(len, files.values())) > MAX_ARCHIVE_BYTES:
                raise ItxError("Тест превышает лимиты размера")
        return path, files["test.xml"], files
    if not path.exists():
        raise ItxError("Файл не найден: %s" % path)
    try:
        with zipfile.ZipFile(str(path), "r") as zf:
            infos = zf.infolist()
            if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
                raise ItxError("Архив превышает лимиты количества файлов или размера")
            seen: Set[str] = set()
            for info in infos:
                name = info.filename.rstrip("/")
                if not safe_archive_path(name) or stat.S_ISLNK(info.external_attr >> 16):
                    raise ItxError("Небезопасный ZIP member: %r" % info.filename)
                if info.is_dir():
                    continue
                if name.casefold() in seen:
                    raise ItxError("Повторяющееся имя ZIP member: %r" % name)
                seen.add(name.casefold())
                if name != "test.xml" and not (safe_image_path(name) or safe_data_path(name)):
                    raise ItxError("Неподдерживаемый ZIP member: %r" % name)
                limit = MAX_XML_BYTES if name == "test.xml" else MAX_MEMBER_BYTES
                if info.file_size > limit:
                    raise ItxError("ZIP member превышает лимит размера")
            bad = zf.testzip()
            if bad:
                raise ItxError("Повреждённый ZIP member: %s" % bad)
            names = zf.namelist()
            if "test.xml" not in names:
                raise ItxError("Архив не содержит test.xml")
            files = {n: zf.read(n) for n in names if not n.endswith("/")}
            return path, files["test.xml"], files
    except zipfile.BadZipFile as exc:
        raise ItxError("Не удалось прочитать ZIP-архив .itx: %s" % exc)


def parse_xml(xml_bytes: bytes) -> ET.Element:
    if len(xml_bytes) > MAX_XML_BYTES:
        raise ItxError("XML превышает лимит размера")
    try:
        xml_text = xml_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ItxError("test.xml должен использовать UTF-8") from exc
    if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", xml_text, re.I):
        raise ItxError("DTD и XML entities не поддерживаются")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ItxError("Некорректный XML: %s" % exc)
    return root


def extract_script(question: ET.Element) -> str:
    lines = [s.get("line", "") for s in question.findall("./modifiers/modifier[@type='script']/script")]
    return "\n".join(lines)


def all_question_nodes(root: ET.Element) -> Iterable[ET.Element]:
    return root.findall(".//question")


def question_location(q: ET.Element) -> str:
    return "question tag=%s type=%s" % (q.get("tag", "?"), q.get("type", "?"))


def _tokens_without_comments(script: str) -> Tuple[str, List[Finding]]:
    """Remove Pascal comments while checking strings and comment delimiters."""
    out: List[str] = []
    findings: List[Finding] = []
    i = 0
    state = "code"
    comment_start = ""
    while i < len(script):
        c = script[i]
        n = script[i + 1] if i + 1 < len(script) else ""
        if state == "code":
            if c == "'":
                out.append(c)
                state = "string"
            elif c == "{" :
                state = "brace"
                comment_start = "{"
                out.append(" ")
            elif c == "(" and n == "*":
                state = "paren"
                comment_start = "(*"
                out.extend([" ", " "])
                i += 1
            elif c == "/" and n == "/":
                out.extend([" ", " "])
                i += 1
                while i + 1 < len(script) and script[i] not in "\r\n":
                    out.append(" ")
                    i += 1
            else:
                out.append(c)
        elif state == "string":
            out.append(" ")
            if c == "'":
                if n == "'":
                    out.append(" ")
                    i += 1
                else:
                    state = "code"
        elif state == "brace":
            out.append("\n" if c in "\r\n" else " ")
            if c == "}":
                state = "code"
        elif state == "paren":
            out.append("\n" if c in "\r\n" else " ")
            if c == "*" and n == ")":
                out.append(" ")
                i += 1
                state = "code"
        i += 1
    if state != "code":
        findings.append(finding("error", "unclosed_comment_or_string", "Незакрытый комментарий или строковый литерал.", "scenario"))
    return "".join(out), findings


def declared_identifiers(script_code: str) -> Set[str]:
    """Best-effort Pascal declaration scan used for useful static diagnostics."""
    declared: Set[str] = set()
    # function/procedure names and their parameters
    for m in re.finditer(r"\b(?:function|procedure)\s+([A-Za-z_]\w*)\s*(?:\((.*?)\))?", script_code, re.I | re.S):
        declared.add(m.group(1))
        params = m.group(2) or ""
        for part in params.split(";"):
            if ":" in part:
                names = part.split(":", 1)[0].replace("var", " ").replace("const", " ")
                declared.update(IDENT_RE.findall(names))
    # var/const declarations up to the next begin/type/function/implementation area.
    for block in re.finditer(r"\b(?:var|const)\b(.*?)(?=\bbegin\b|\bfunction\b|\bprocedure\b|\btype\b|$)", script_code, re.I | re.S):
        for line in block.group(1).split(";"):
            if ":" in line:
                names = line.split(":", 1)[0]
                declared.update(IDENT_RE.findall(names))
    declared.add("Result")
    return declared


def script_findings(question: ET.Element, marker_names: Set[str]) -> List[Finding]:
    script = extract_script(question)
    if not marker_names and not script:
        return []
    if marker_names and not script:
        return [finding("error", "placeholder_without_script", "В тексте есть маркеры $(...), но сценарий не задан.", question_location(question))]
    code, fs = _tokens_without_comments(script)
    loc = question_location(question)
    # Pascal structural checks.  The editor accepts several dialect extensions,
    # so these checks intentionally avoid pretending to be a full compiler.
    words = re.findall(r"\b(begin|end|repeat|until|case)\b", code, re.I)
    stack: List[str] = []
    for word in words:
        w = word.lower()
        if w == "begin":
            stack.append("begin")
        elif w == "repeat":
            stack.append("repeat")
        elif w == "case":
            stack.append("case")
        elif w == "until":
            if "repeat" in stack:
                while stack and stack[-1] != "repeat":
                    stack.pop()
                if stack:
                    stack.pop()
            else:
                fs.append(finding("error", "unmatched_until", "Оператор until без соответствующего repeat.", loc))
        elif w == "end":
            if stack:
                stack.pop()
            # an unmatched end is usually a real syntax error; allow end. as token too.
            else:
                fs.append(finding("error", "unmatched_end", "Оператор end без соответствующего begin/case.", loc))
    if any(x == "repeat" for x in stack):
        fs.append(finding("error", "unclosed_repeat", "Есть repeat без until.", loc))
    if any(x in {"begin", "case"} for x in stack):
        fs.append(finding("warning", "unclosed_begin", "Не удалось сбалансировать begin/end; проверьте сценарий в Айрен.", loc))
    declared = declared_identifiers(code)
    assignments = set(re.findall(r"\b([A-Za-z_]\w*)\s*:=", code, re.I))
    # A common Айрен idiom passes output variables to a helper procedure:
    # ``Matrix3Split(m, a11, a12, ...)``.  The Pascal compiler knows these
    # parameters are ``var`` parameters, while a text-only checker cannot
    # infer the procedure signature.  Treat simple identifiers in calls as
    # possible outputs to avoid noisy false warnings for that idiom.
    for call in re.finditer(r"\b[A-Za-z_]\w*\s*\(([^()]*)\)", code, re.S):
        for candidate in IDENT_RE.findall(call.group(1)):
            if candidate in declared:
                assignments.add(candidate)
    for name in sorted(marker_names):
        if name not in declared:
            fs.append(finding("error", "undeclared_placeholder", "Метка $(%s) не соответствует объявленной переменной сценария." % name, loc))
        elif name not in assignments and name != "Result":
            fs.append(finding("warning", "unassigned_placeholder", "Переменная $(%s) объявлена, но статически не найдено присваивание; проверьте все ветви сценария." % name, loc))
    return fs


def marker_names_for_question(q: ET.Element) -> Set[str]:
    chunks: List[str] = []
    for e in q.findall(".//text"):
        chunks.append(e.get("value", ""))
    for e in q.findall(".//pattern"):
        chunks.append(e.get("value", ""))
    return set(re.findall(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", "\n".join(chunks)))


def validate_formulas(source: str) -> Dict[str, Any]:
    path, xml_bytes, _ = read_itx(source)
    root = parse_xml(xml_bytes)
    fs: List[Finding] = []
    formula_count = 0
    pattern_count = 0
    script_count = 0
    for q in all_question_nodes(root):
        loc = question_location(q)
        for f in q.findall(".//formula"):
            formula_count += 1
            val = f.get("source", "")
            if not val.strip():
                fs.append(finding("error", "empty_formula", "Пустой атрибут formula/@source.", loc))
            if len(val) > 1000:
                fs.append(finding("error", "formula_too_long", "Айрен ограничивает inline-формулу 1000 символами.", loc))
            if any(ord(ch) < 32 and ch not in "\r\n\t" for ch in val):
                fs.append(finding("error", "formula_control_character", "Формула содержит управляющий символ.", loc))
            # Catch obvious unbalanced delimiters without imposing a TeX dialect.
            for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
                if val.count(opening) != val.count(closing):
                    fs.append(finding("error", "unbalanced_formula", "Несбалансированные скобки в формуле %r." % val, loc))
                    break
        for p in q.findall(".//pattern"):
            pattern_count += 1
            locp = "%s pattern" % loc
            val = p.get("value", "")
            if not val.strip():
                fs.append(finding("error", "empty_pattern", "Пустой эталон ответа.", locp))
            pattern_type = p.get("type", "")
            if pattern_type not in {"text", "regexp"}:
                fs.append(finding("error", "invalid_pattern_type", "Атрибут type должен быть text или regexp.", locp))
            for attr in ("caseSensitive", "numericAware", "wildcard"):
                if attr in p.attrib and as_bool(p.get(attr)) is None:
                    fs.append(finding("error", "invalid_pattern_boolean", "Атрибут %s должен быть true/false." % attr, locp))
            quality = p.get("quality")
            if quality is None or not re.fullmatch(r"[0-9]\.[0-9]{4}", quality) or not 0 <= float(quality) <= 1:
                fs.append(finding("error", "invalid_pattern_quality", "Атрибут quality должен иметь вид 0.0000 и находиться в диапазоне от 0 до 1.", locp))
            if pattern_type == "text" and as_bool(p.get("numericAware")) is True:
                precision = p.get("precision")
                # Айрен принимает только десятичную запись из цифр и точки;
                # экспоненциальная форма вроде 1e-06 приводит к ошибке открытия.
                if precision is None or len(precision) > 10 or not re.fullmatch(r"[0-9.]+", precision):
                    fs.append(finding("error", "invalid_pattern_precision", "Для numericAware-шаблона precision должна быть десятичной строкой из цифр и точки длиной не более 10 символов.", locp))
                else:
                    try:
                        float(precision)
                    except ValueError:
                        fs.append(finding("error", "invalid_pattern_precision", "Атрибут precision имеет недопустимое десятичное значение.", locp))
            spaces = p.get("spaces")
            allowed_spaces = {"normalize", "ignore"} if pattern_type == "text" else {"exact", "normalize", "ignore"}
            if spaces not in allowed_spaces:
                fs.append(finding("error", "invalid_pattern_spaces", "Недопустимое значение spaces для данного типа шаблона.", locp))
        # Detect malformed placeholders separately from valid markers.
        for e in q.findall(".//text") + q.findall(".//pattern"):
            value = e.get("value", "")
            if "$" in value:
                for marker in re.finditer(r"\$\(", value):
                    tail = value[marker.start():]
                    if not re.match(r"\$\([A-Za-z_][A-Za-z0-9_]*\)", tail):
                        fs.append(finding("error", "malformed_placeholder", "Неверный маркер переменной; используйте $(имя).", loc))
                        break
        script = extract_script(q)
        if script:
            script_count += 1
        fs.extend(script_findings(q, marker_names_for_question(q)))
    errors = [x for x in fs if x.severity == "error"]
    return {
        "path": str(path),
        "valid": not errors,
        "formulaCount": formula_count,
        "patternCount": pattern_count,
        "scriptCount": script_count,
        "findings": [x.as_dict() for x in fs],
    }


def _required_content(q: ET.Element, fs: List[Finding]) -> None:
    if not content_text(q.find("content")).strip():
        fs.append(finding("error", "empty_question_content", "У вопроса отсутствует непустое условие.", question_location(q)))


def _validate_profiles(root: ET.Element, fs: List[Finding]) -> None:
    for profile in root.findall(".//profile"):
        title = profile.get("title", "")
        loc = "profile %r" % title
        qs = profile.find("questionSelection")
        if qs is None:
            fs.append(finding("error", "profile_without_selection", "Профиль не содержит questionSelection.", loc))
        so = profile.find("sessionOptions")
        if so is not None:
            duration = so.get("durationMinutes")
            if duration not in (None, "unlimited"):
                try:
                    if float(duration) < 0:
                        raise ValueError()
                except ValueError:
                    fs.append(finding("error", "invalid_duration", "durationMinutes должен быть unlimited или неотрицательным числом.", loc))
        bounds: List[float] = []
        for mark in profile.findall("./markScale/marks/mark"):
            raw_bound = mark.get("lowerBound", "")
            if not re.fullmatch(r"(?:0\.[0-9]{4}|1\.0000)", raw_bound):
                fs.append(finding("error", "invalid_mark_bound", "lowerBound должен иметь вид 0.0000..0.9999 или 1.0000.", loc))
            else:
                bounds.append(float(raw_bound))
        if bounds and bounds[0] != 0:
            fs.append(finding("error", "mark_scale_must_start_at_zero", "Первая граница шкалы оценок должна быть 0.0000.", loc))
        if bounds != sorted(bounds) or len(set(bounds)) != len(bounds):
            fs.append(finding("error", "marks_not_increasing", "Границы шкалы оценок должны возрастать.", loc))
        for sp in profile.findall(".//sectionProfile"):
            value = sp.get("questions")
            if value and value != "all":
                integer = re.fullmatch(r"\d+", value)
                percent = re.fullmatch(r"(\d+(?:\.\d+)?)%", value)
                if integer:
                    if not 0 <= int(value) <= 999:
                        fs.append(finding("error", "invalid_section_questions", "questions в sectionProfile должен быть целым числом 0..999.", loc))
                elif not percent or not 0 <= float(percent.group(1)) <= 100:
                    fs.append(finding("error", "invalid_section_questions", "questions в sectionProfile должен быть целым числом 0..999, процентом 0..100% или all.", loc))


def _section_children(section: ET.Element) -> List[ET.Element]:
    container = section.find("sections")
    return container.findall("section") if container is not None else []


def _section_profile_mismatches(section: ET.Element, profile: ET.Element, location: str) -> List[Finding]:
    findings: List[Finding] = []
    if profile.get("title") != section.get("title"):
        findings.append(finding("error", "section_profile_name_mismatch", "Название sectionProfile должно совпадать с названием section.", location))
    children = _section_children(section)
    profiles = profile.find("sectionProfiles")
    profile_children = profiles.findall("sectionProfile") if profiles is not None else []
    if len(children) != len(profile_children):
        findings.append(finding("error", "section_profile_tree_mismatch", "Число вложенных sectionProfile должно совпадать с числом вложенных section.", location))
    for child, child_profile in zip(children, profile_children):
        findings.extend(_section_profile_mismatches(child, child_profile, location))
    return findings


def _validate_profile_source_constraints(root: ET.Element, fs: List[Finding]) -> None:
    sections = root.findall("./section")
    for profile in root.findall("./profiles/profile"):
        loc = "profile %r" % profile.get("title", "")
        selection = profile.find("questionSelection")
        section_profile = profile.find("sectionProfile")
        if selection is None:
            continue
        has_qps = "questionsPerSection" in selection.attrib
        if section_profile is None and not has_qps:
            fs.append(finding("error", "missing_questions_per_section", "Без sectionProfile профиль должен содержать questionsPerSection.", loc))
        if section_profile is not None and has_qps:
            fs.append(finding("error", "conflicting_profile_selection", "Профиль не может одновременно содержать sectionProfile и questionsPerSection.", loc))
        if has_qps:
            value = selection.get("questionsPerSection", "")
            if value != "all":
                try:
                    if int(value) < 1:
                        raise ValueError()
                except ValueError:
                    fs.append(finding("error", "invalid_questions_per_section", "questionsPerSection должен быть all или положительным целым числом.", loc))
        session = profile.find("sessionOptions")
        if session is None:
            fs.append(finding("error", "missing_session_options", "Профиль должен содержать sessionOptions.", loc))
        else:
            required = ("durationMinutes", "editableAnswers", "browsableQuestions", "weightCues")
            for attr in required:
                if attr not in session.attrib:
                    fs.append(finding("error", "missing_session_attribute", "В sessionOptions отсутствует обязательный атрибут %s." % attr, loc))
            duration = session.get("durationMinutes")
            if duration not in (None, "unlimited"):
                try:
                    if int(duration) < 1:
                        raise ValueError()
                except ValueError:
                    fs.append(finding("error", "invalid_duration", "durationMinutes должен быть unlimited или положительным целым числом.", loc))
            for attr in ("editableAnswers", "browsableQuestions", "weightCues"):
                if attr in session.attrib and as_bool(session.get(attr)) is None:
                    fs.append(finding("error", "invalid_session_boolean", "Атрибут %s должен быть true/false." % attr, loc))
        if section_profile is not None and sections:
            fs.extend(_section_profile_mismatches(sections[0], section_profile, loc))


def _png_dimensions(data: bytes) -> Optional[Tuple[int, int]]:
    signature = b"\x89PNG\r\n\x1a\n"
    if len(data) < 24 or data[:8] != signature or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        return None
    return width, height


def _png_is_uniform(data: bytes) -> Optional[bool]:
    """Best-effort blank/solid PNG check with a decompression-pixel limit."""
    try:
        if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        width, height = _png_dimensions(data) or (0, 0)
        if not width or not height:
            return None
        bit_depth, color_type, interlace = data[24], data[25], data[28]
        if bit_depth != 8 or interlace != 0 or color_type not in (0, 2, 3, 4, 6):
            return None
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
        palette: Optional[bytes] = None
        compressed = bytearray()
        pos = 8
        while pos + 12 <= len(data):
            length = int.from_bytes(data[pos:pos + 4], "big")
            kind = data[pos + 4:pos + 8]
            body_start = pos + 8
            body_end = body_start + length
            if body_end + 4 > len(data):
                return None
            body = data[body_start:body_end]
            pos = body_end + 4
            if kind == b"PLTE":
                palette = body
            elif kind == b"IDAT":
                compressed.extend(body)
            elif kind == b"IEND":
                break
        import zlib
        raw = zlib.decompress(bytes(compressed))
        stride = width * channels
        if len(raw) != (stride + 1) * height:
            return None
        previous = bytearray(stride)
        first: Optional[bytes] = None
        for row in range(height):
            offset = row * (stride + 1)
            filter_type = raw[offset]
            line = bytearray(raw[offset + 1:offset + 1 + stride])
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up = previous[i]
                upper_left = previous[i - channels] if i >= channels else 0
                if filter_type == 1:
                    line[i] = (line[i] + left) & 255
                elif filter_type == 2:
                    line[i] = (line[i] + up) & 255
                elif filter_type == 3:
                    line[i] = (line[i] + ((left + up) // 2)) & 255
                elif filter_type == 4:
                    estimate = left + up - upper_left
                    pa, pb, pc = abs(estimate - left), abs(estimate - up), abs(estimate - upper_left)
                    predictor = left if pa <= pb and pa <= pc else (up if pb <= pc else upper_left)
                    line[i] = (line[i] + predictor) & 255
                elif filter_type != 0:
                    return None
            previous = line
            for col in range(width):
                pixel = bytes(line[col * channels:(col + 1) * channels])
                if color_type == 3:
                    index = pixel[0] * 3
                    pixel = palette[index:index + 3] if palette and index + 3 <= len(palette) else b"\x00\x00\x00"
                elif color_type in (0, 4):
                    pixel = bytes([pixel[0]] * 3)
                if first is None:
                    first = pixel
                elif pixel != first:
                    return False
        return True
    except Exception:
        return None


def _validate_images(root: ET.Element, files: Dict[str, bytes], fs: List[Finding]) -> Dict[str, Dict[str, Any]]:
    stats: Dict[str, Dict[str, Any]] = {}
    parent = {child: parent for parent in root.iter() for child in parent}
    answer_tags = {"choice", "baseItem", "matchingItem", "distractor", "sequenceItem", "categoryItem"}
    for image in root.findall(".//img"):
        src = image.get("src", "")
        if src in stats or src not in files:
            continue
        data = files[src]
        record: Dict[str, Any] = {"bytes": len(data)}
        dims = _png_dimensions(data) if src.lower().endswith(".png") else None
        if dims:
            record["width"], record["height"] = dims
            ancestors: List[str] = []
            node = parent.get(image)
            while node is not None:
                ancestors.append(node.tag)
                node = parent.get(node)
            in_answer = bool(set(ancestors) & answer_tags)
            max_w, max_h = (1200, 300) if in_answer else (1200, 400)
            if dims[0] < 24 or dims[1] < 24:
                fs.append(finding("warning", "image_too_small", "Изображение меньше рекомендуемого минимума 24x24.", src))
            if dims[0] > max_w or dims[1] > max_h:
                fs.append(finding("warning", "image_too_large", "Изображение превышает рекомендуемый размер для Айрен (%dx%d)." % (max_w, max_h), src))
            uniform = _png_is_uniform(data)
            if uniform:
                fs.append(finding("warning", "image_uniform", "PNG однороден и может быть пустым или нечитаемым.", src))
                record["uniform"] = True
        if not src.startswith("images/") or any(ord(ch) > 127 for ch in src):
            fs.append(finding("warning", "image_name_quality", "Для переносимости используйте ASCII-путь images/filename.png или images/filename.jpg.", src))
        stats[src] = record
    return stats


def validate_test(source: str, check_near_dups: bool = False) -> Dict[str, Any]:
    path, xml_bytes, files = read_itx(source)
    root = parse_xml(xml_bytes)
    fs: List[Finding] = []
    if root.tag != "irenTest":
        fs.append(finding("error", "wrong_root", "Корневой элемент должен быть irenTest."))
    if root.get("version") != ITX_VERSION:
        fs.append(finding("error", "unsupported_version", "Ожидается irenTest version=\"6\"."))
    if not root.findall("./section"):
        fs.append(finding("error", "no_sections", "В тесте нет корневого section."))
    for section in root.findall(".//section"):
        if len(section.get("title", "")) > 100:
            fs.append(finding("error", "section_title_too_long", "Название section не должно превышать 100 символов.", section.get("title", "")))
    tags: Set[str] = set()
    max_tag = 0
    referenced_images: Set[str] = set()
    referenced_data: Set[str] = set()
    parent = {child: parent for parent in root.iter() for child in parent}

    def inherited_modifier(q: ET.Element, modifier_type: str) -> bool:
        node = q
        while node is not None:
            if node.tag in {"question", "section", "profile"}:
                if any(m.get("type") == modifier_type for m in node.findall("./modifiers/modifier")):
                    return True
            node = parent.get(node)
        return False
    for img in root.findall(".//img"):
        src = img.get("src", "")
        safe = safe_image_path(src)
        if not safe:
            fs.append(finding("error", "unsafe_image_path", "Небезопасный путь изображения: %r." % src))
        else:
            referenced_images.add(safe)
        clipboard = img.get("clipboardData")
        if clipboard is not None:
            safe_data = safe_data_path(clipboard)
            if not safe_data:
                fs.append(finding("error", "unsafe_clipboard_data_path", "Небезопасный путь исходных данных изображения: %r." % clipboard))
            else:
                referenced_data.add(safe_data)
    for q in all_question_nodes(root):
        loc = question_location(q)
        _required_content(q, fs)
        qtype = q.get("type", "")
        if qtype not in QUESTION_TYPES:
            fs.append(finding("error", "unknown_question_type", "Неизвестный тип вопроса: %s." % qtype, loc))
        tag = q.get("tag")
        if tag is not None and tag != "":
            if not all(ch.isalnum() or ch == "_" for ch in tag):
                fs.append(finding("error", "invalid_tag", "tag должен содержать только буквы, цифры и _.", loc))
            if tag in tags:
                fs.append(finding("error", "duplicate_tag", "Тег вопроса повторяется: %s." % tag, loc))
            tags.add(tag)
            if tag.isdigit():
                max_tag = max(max_tag, int(tag))
        if q.get("enabled") is None:
            fs.append(finding("error", "missing_enabled", "У вопроса отсутствует обязательный атрибут enabled.", loc))
        elif as_bool(q.get("enabled"), None) is None:
            fs.append(finding("error", "invalid_enabled", "enabled должен быть true/false.", loc))
        try:
            weight_text = q.get("weight", "")
            if not re.fullmatch(r"[0-9]+", weight_text) or not 1 <= int(weight_text) <= 1000:
                raise ValueError()
        except ValueError:
            fs.append(finding("error", "invalid_weight", "weight должен быть целым числом от 1 до 1000.", loc))
        if qtype == "select":
            choices = q.findall("./choices/choice")
            if len(choices) < 2:
                fs.append(finding("error", "too_few_choices", "Select-вопрос должен содержать не менее двух вариантов.", loc))
            if len(choices) > 20:
                fs.append(finding("error", "too_many_choices", "Select-вопрос не может содержать более 20 вариантов.", loc))
            correct = [c for c in choices if as_bool(c.get("correct"), False)]
            modifiers = {m.get("type") for m in q.findall("./modifiers/modifier")}
            enabled = as_bool(q.get("enabled"), True)
            if not correct and "addNegativeChoice" not in modifiers and not inherited_modifier(q, "addNegativeChoice"):
                severity = "error" if enabled is not False else "warning"
                fs.append(finding(severity, "no_correct_choice", "Нет правильного варианта ответа.", loc))
            texts = [content_text(c.find("content")).strip() for c in choices]
            if any(not t for t in texts):
                fs.append(finding("error", "empty_choice", "Вариант ответа пуст.", loc))
            if len(texts) != len(set(texts)):
                fs.append(finding("warning", "duplicate_choice", "Есть одинаковые варианты ответа; это делает выбор неоднозначным.", loc))
            if check_near_dups:
                for left in range(len(texts)):
                    for right in range(left + 1, len(texts)):
                        if texts[left] and texts[right] and texts[left] != texts[right] and levenshtein(texts[left].lower(), texts[right].lower(), cap=2) <= 2:
                            fs.append(finding("warning", "near_duplicate_choice", "Варианты ответа отличаются на 1–2 символа; проверьте, не является ли это опечаткой.", loc))
            for c in choices:
                for attr in ("correct", "fixed", "negative"):
                    if as_bool(c.get(attr), None) is None:
                        fs.append(finding("error", "invalid_choice_boolean", "Атрибут %s должен быть true/false." % attr, loc))
        elif qtype == "input":
            patterns = q.findall("./patterns/pattern")
            if not patterns:
                fs.append(finding("error", "no_patterns", "Input-вопрос должен содержать patterns/pattern.", loc))
            if len(patterns) > 20:
                fs.append(finding("error", "too_many_patterns", "Input-вопрос не может содержать более 20 шаблонов.", loc))
            for pattern in patterns:
                if len(pattern.get("value", "")) > 200:
                    fs.append(finding("error", "pattern_too_long", "Эталон input-ответа не может превышать 200 символов.", loc))
        elif qtype == "match":
            pairs = q.findall("./pairs/pair")
            if len(pairs) < 2:
                fs.append(finding("error", "too_few_pairs", "Match-вопрос должен содержать не менее двух пар.", loc))
            if len(pairs) + len(q.findall("./distractors/distractor")) > 50:
                fs.append(finding("error", "too_many_match_rows", "Match-вопрос не может содержать более 50 строк.", loc))
            bases = [content_text(p.find("baseItem/content")).strip() for p in pairs]
            matches = [content_text(p.find("matchingItem/content")).strip() for p in pairs]
            if any(not x for x in bases + matches):
                fs.append(finding("error", "empty_pair_item", "Элемент пары не может быть пустым.", loc))
            if len(bases) != len(set(bases)) or len(matches) != len(set(matches)):
                fs.append(finding("warning", "duplicate_pair_item", "В match-вопросе повторяются элементы пары.", loc))
        elif qtype == "order":
            seq = [content_text(x.find("content")).strip() for x in q.findall("./sequence/sequenceItem")]
            dis = [content_text(x.find("content")).strip() for x in q.findall("./distractors/distractor")]
            if len(seq) < 2:
                fs.append(finding("error", "too_few_sequence_items", "Order-вопрос должен содержать не менее двух элементов.", loc))
            if len(seq) + len(dis) > 50:
                fs.append(finding("error", "too_many_order_items", "Order-вопрос не может содержать более 50 элементов.", loc))
            if any(not x for x in seq + dis):
                fs.append(finding("error", "empty_sequence_item", "Элемент последовательности не может быть пустым.", loc))
            if len(seq) != len(set(seq)) or set(seq) & set(dis) or len(dis) != len(set(dis)):
                fs.append(finding("error", "duplicate_order_item", "Элементы order-вопроса должны быть уникальны.", loc))
        elif qtype == "classify":
            cats = q.findall("./categories/category")
            if len(cats) < 2:
                fs.append(finding("error", "too_few_categories", "Classify-вопрос должен содержать не менее двух категорий.", loc))
            if len(cats) > 10:
                fs.append(finding("error", "too_many_categories", "Classify-вопрос не может содержать более 10 категорий.", loc))
            titles = [content_text(c.find("categoryTitle/content")).strip() for c in cats]
            items = [content_text(i.find("content")).strip() for c in cats for i in c.findall("./categoryItems/categoryItem")]
            if len(items) > 100:
                fs.append(finding("error", "too_many_classify_items", "Classify-вопрос не может содержать более 100 элементов.", loc))
            if any(not x for x in titles):
                fs.append(finding("error", "empty_category_title", "Название категории не может быть пустым.", loc))
            if any(not x for x in items):
                fs.append(finding("error", "empty_category_item", "Элемент классификации не может быть пустым.", loc))
            if len(titles) != len(set(titles)) or len(items) != len(set(items)):
                fs.append(finding("error", "duplicate_classify_item", "Категории и элементы классификации должны быть уникальны.", loc))
    for image in sorted(referenced_images):
        if image not in files:
            fs.append(finding("error", "missing_image", "Изображение, указанное в XML, отсутствует в архиве: %s." % image))
    for data_path in sorted(referenced_data):
        if data_path not in files:
            fs.append(finding("error", "missing_clipboard_data", "Исходные данные изображения отсутствуют в архиве: %s." % data_path))
    image_stats = _validate_images(root, files, fs)
    try:
        last_tag = int(root.findtext("./testInfo/@lastGeneratedTag"))  # type: ignore[arg-type]
    except Exception:
        info = root.find("./testInfo")
        try:
            last_tag = int(info.get("lastGeneratedTag", "")) if info is not None else 0
        except ValueError:
            last_tag = 0
    if root.find("./testInfo") is None:
        fs.append(finding("warning", "missing_test_info", "Нет testInfo/lastGeneratedTag; редактор обычно создаёт его автоматически."))
    elif last_tag < max_tag:
        fs.append(finding("error", "stale_last_generated_tag", "lastGeneratedTag меньше максимального тега вопроса."))
    _validate_profiles(root, fs)
    _validate_profile_source_constraints(root, fs)
    formula_report = validate_formulas(str(path))
    # Do not duplicate formula findings in full report; retain them under a separate field.
    errors = [x for x in fs if x.severity == "error"]
    return {
        "path": str(path),
        "valid": not errors and formula_report["valid"],
        "questionCount": len(list(all_question_nodes(root))),
        "questionTypes": {t: sum(1 for q in all_question_nodes(root) if q.get("type") == t) for t in sorted(QUESTION_TYPES)},
        "imageCount": len([n for n in files if safe_image_path(n)]),
        "imageStats": image_stats,
        "nearDuplicateCheck": check_near_dups,
        "findings": [x.as_dict() for x in fs],
        "formulaValidation": formula_report,
    }


def segment(value: Any, parent: ET.Element) -> None:
    """Append a compact content DSL to parent.

    Strings become text.  A list can contain strings or dictionaries with one
    of text/img/br/formula keys.
    """
    if value is None:
        return
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, str):
            ET.SubElement(parent, "text", {"value": item})
        elif isinstance(item, dict):
            if "text" in item:
                ET.SubElement(parent, "text", {"value": str(item["text"])})
            elif "img" in item:
                ET.SubElement(parent, "img", {"src": str(item["img"])})
            elif "formula" in item:
                ET.SubElement(parent, "formula", {"source": str(item["formula"])})
            elif item.get("br"):
                ET.SubElement(parent, "br")
            else:
                raise ItxError("Неизвестный сегмент content: %r" % item)
        else:
            raise ItxError("Content-сегмент должен быть строкой или объектом: %r" % item)


def add_content(parent: ET.Element, value: Any) -> ET.Element:
    c = ET.SubElement(parent, "content")
    segment(value, c)
    return c


def _bool(v: Any, default: bool = False) -> str:
    """Serialize a JSON boolean-like value using Ayren's true/false syntax.

    Specs often come from generated JSON and may contain the strings
    ``"true"``/``"false"``.  Python's ``bool("false")`` is ``True``, which
    silently produced an incorrect test.  Keep real booleans/numbers useful,
    but reject unknown strings instead of guessing.
    """
    value = default if v is None else v
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return "true"
        if normalized == "false":
            return "false"
        raise ItxError("Логическое значение должно быть true или false: %r" % value)
    return "true" if bool(value) else "false"


def _choice(parent: ET.Element, spec: Dict[str, Any]) -> None:
    attrs = {k: _bool(spec.get(k), False) for k in ("correct", "fixed", "negative")}
    c = ET.SubElement(parent, "choice", attrs)
    add_content(c, spec.get("content", spec.get("text", "")))


def build_xml(spec: Dict[str, Any]) -> Tuple[ET.Element, Dict[str, bytes]]:
    root = ET.Element("irenTest", {"version": str(spec.get("version", ITX_VERSION))})
    images: Dict[str, bytes] = {}
    sections = spec.get("sections") or [{"title": spec.get("sectionTitle", ""), "questions": spec.get("questions", [])}]
    next_tag = 1
    max_tag = 0

    def build_section(parent: ET.Element, section_spec: Dict[str, Any]) -> None:
        nonlocal next_tag, max_tag
        s = ET.SubElement(parent, "section", {"title": str(section_spec.get("title", ""))})
        questions = section_spec.get("questions", [])
        if questions:
            qparent = ET.SubElement(s, "questions")
            for qs in questions:
                qtype = str(qs.get("type", "select"))
                if qtype not in QUESTION_TYPES:
                    raise ItxError("Неизвестный type вопроса: %s" % qtype)
                tag = int(qs.get("tag", next_tag))
                next_tag = max(next_tag, tag + 1)
                max_tag = max(max_tag, tag)
                q = ET.SubElement(qparent, "question", {"type": qtype, "tag": str(tag), "weight": str(qs.get("weight", 1)), "enabled": _bool(qs.get("enabled"), True)})
                add_content(q, qs.get("content", qs.get("prompt", "")))
                if qtype == "select":
                    choices = ET.SubElement(q, "choices")
                    for cs in qs.get("choices", []):
                        _choice(choices, cs if isinstance(cs, dict) else {"text": str(cs)})
                elif qtype == "input":
                    pats = ET.SubElement(q, "patterns")
                    for ps in qs.get("patterns", []):
                        ps = ps if isinstance(ps, dict) else {"value": str(ps)}
                        attrs = {"type": ps.get("type", "text"), "value": str(ps.get("value", "")), "quality": str(ps.get("quality", "1.0000")), "caseSensitive": _bool(ps.get("caseSensitive"), False), "spaces": str(ps.get("spaces", "normalize")), "numericAware": _bool(ps.get("numericAware"), False), "wildcard": _bool(ps.get("wildcard"), False)}
                        if "precision" in ps:
                            attrs["precision"] = str(ps["precision"])
                        ET.SubElement(pats, "pattern", attrs)
                elif qtype == "match":
                    mo = qs.get("matchOptions", {})
                    ET.SubElement(q, "matchOptions", {"baseItemsUsed": str(mo.get("baseItemsUsed", "all")), "distractorsUsed": str(mo.get("distractorsUsed", "all"))})
                    pairs = ET.SubElement(q, "pairs")
                    for pair in qs.get("pairs", []):
                        p = ET.SubElement(pairs, "pair")
                        add_content(ET.SubElement(p, "baseItem"), pair.get("base", pair.get("baseItem", "")))
                        add_content(ET.SubElement(p, "matchingItem"), pair.get("match", pair.get("matchingItem", "")))
                    if qs.get("distractors"):
                        ds = ET.SubElement(q, "distractors")
                        for d in qs["distractors"]:
                            add_content(ET.SubElement(ds, "distractor"), d)
                elif qtype == "order":
                    oo = qs.get("orderOptions", {})
                    ET.SubElement(q, "orderOptions", {"sequenceItemsUsed": str(oo.get("sequenceItemsUsed", "all")), "distractorsUsed": str(oo.get("distractorsUsed", "all"))})
                    seq = ET.SubElement(q, "sequence")
                    for item in qs.get("sequence", []):
                        add_content(ET.SubElement(seq, "sequenceItem"), item)
                    if qs.get("distractors"):
                        ds = ET.SubElement(q, "distractors")
                        for d in qs["distractors"]:
                            add_content(ET.SubElement(ds, "distractor"), d)
                elif qtype == "classify":
                    co = qs.get("classifyOptions", {})
                    ET.SubElement(q, "classifyOptions", {"itemsUsed": str(co.get("itemsUsed", "all")), "minItemsPerCategoryUsed": str(co.get("minItemsPerCategoryUsed", "all"))})
                    cats = ET.SubElement(q, "categories")
                    for cat in qs.get("categories", []):
                        c = ET.SubElement(cats, "category")
                        add_content(ET.SubElement(c, "categoryTitle"), cat.get("title", ""))
                        cis = ET.SubElement(c, "categoryItems")
                        for item in cat.get("items", []):
                            add_content(ET.SubElement(cis, "categoryItem"), item)
                if qs.get("modifiers") or qs.get("script"):
                    mods = ET.SubElement(q, "modifiers")
                    for m in qs.get("modifiers", []):
                        ET.SubElement(mods, "modifier", {"type": str(m) if isinstance(m, str) else str(m.get("type", ""))})
                    if qs.get("script"):
                        mod = ET.SubElement(mods, "modifier", {"type": "script"})
                        script = qs["script"] if isinstance(qs["script"], list) else str(qs["script"]).splitlines()
                        for line in script:
                            ET.SubElement(mod, "script", {"line": str(line)})
            # Nested sections are emitted after questions, matching files made by the editor.
        children = section_spec.get("sections", [])
        if children:
            sp = ET.SubElement(s, "sections")
            for child in children:
                build_section(sp, child)

    for section in sections:
        build_section(root, section)
    profiles = spec.get("profiles")
    if profiles:
        ps = ET.SubElement(root, "profiles")
        for p in profiles:
            profile = ET.SubElement(ps, "profile", {"title": str(p.get("title", ""))})
            if p.get("questionSelection") is not None:
                ET.SubElement(profile, "questionSelection", {str(k): str(v) for k, v in p["questionSelection"].items()})
            if p.get("sessionOptions") is not None:
                ET.SubElement(profile, "sessionOptions", {str(k): str(v) for k, v in p["sessionOptions"].items()})
            for name in ("instantFeedback", "testResults"):
                if p.get(name) is not None:
                    ET.SubElement(profile, name, {str(k): str(v) for k, v in p[name].items()})
            if p.get("questionResults") is not None:
                tr = profile.find("testResults")
                if tr is None:
                    tr = ET.SubElement(profile, "testResults")
                ET.SubElement(tr, "questionResults", {str(k): str(v) for k, v in p["questionResults"].items()})
            marks = p.get("marks")
            if marks:
                ms = ET.SubElement(profile, "markScale")
                container = ET.SubElement(ms, "marks")
                for mark in marks:
                    ET.SubElement(container, "mark", {"title": str(mark["title"]), "lowerBound": str(mark["lowerBound"])})
            modifiers = p.get("modifiers")
            if modifiers:
                ms = ET.SubElement(profile, "modifiers")
                for m in modifiers:
                    ET.SubElement(ms, "modifier", {"type": str(m) if isinstance(m, str) else str(m.get("type", ""))})
    ET.SubElement(root, "testInfo", {"lastGeneratedTag": str(spec.get("lastGeneratedTag", max_tag))})
    for archive_name, value in (spec.get("images") or {}).items():
        safe = safe_image_path(str(archive_name))
        if not safe:
            raise ItxError("Изображение должно находиться в images/: %s" % archive_name)
        if isinstance(value, str) and Path(value).exists():
            input_path = Path(value)
            if input_path.is_symlink() or not input_path.is_file():
                raise ItxError("Источник изображения должен быть обычным файлом: %s" % value)
            if input_path.stat().st_size > MAX_MEMBER_BYTES:
                raise ItxError("Изображение превышает лимит размера: %s" % value)
            images[safe] = input_path.read_bytes()
        elif isinstance(value, str):
            try:
                images[safe] = base64.b64decode(value, validate=True)
            except Exception:
                raise ItxError("images[%s] — не существующий путь и не base64." % archive_name)
        elif isinstance(value, (bytes, bytearray)):
            images[safe] = bytes(value)
        else:
            raise ItxError("Недопустимое значение images[%s]." % archive_name)
        if len(images[safe]) > MAX_MEMBER_BYTES:
            raise ItxError("Изображение превышает лимит размера: %s" % archive_name)
        if sum(len(item) for item in images.values()) > MAX_ARCHIVE_BYTES:
            raise ItxError("Изображения превышают общий лимит размера теста")
    return root, images


def indent_xml(root: ET.Element) -> bytes:
    try:
        ET.indent(root, space="  ")
    except AttributeError:  # pragma: no cover - Python 3.8 fallback
        pass
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def load_spec(spec: Any) -> Dict[str, Any]:
    if isinstance(spec, dict):
        return spec
    if not isinstance(spec, str):
        raise ItxError("spec должен быть JSON-объектом или путём к JSON-файлу")
    p = Path(spec).expanduser()
    if not p.exists():
        raise ItxError("JSON spec не найден: %s" % p)
    try:
        value = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ItxError("Не удалось прочитать JSON spec: %s" % exc)
    if not isinstance(value, dict):
        raise ItxError("Корень JSON spec должен быть объектом")
    return value


def create_test(args: Dict[str, Any]) -> Dict[str, Any]:
    spec = load_spec(args.get("spec", args))
    output_value = args.get("output") or spec.get("output") or "test.itx"
    output = absolute_path(str(output_value))
    checked_write_path(output, overwrite=bool(args.get("overwrite", False)))
    root, images = build_xml(spec)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(output), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("test.xml", indent_xml(root))
        for name in sorted(images):
            zf.writestr(name, images[name])
    report = validate_test(str(output))
    return {"output": str(output), "validation": report}


def build_test(args: Dict[str, Any]) -> Dict[str, Any]:
    if args.get("spec") is not None:
        return create_test(args)
    source = args.get("input") or args.get("input_itx")
    if not source:
        raise ItxError("build_test требует input_itx или spec")
    src_path, xml_bytes, files = read_itx(str(source))
    output_value = args.get("output") or str(src_path.with_name(src_path.stem + "_built.itx"))
    output = absolute_path(str(output_value))
    checked_write_path(output, overwrite=bool(args.get("overwrite", False)))
    root = parse_xml(xml_bytes)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(output), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("test.xml", indent_xml(root))
        for name in sorted(files):
            if name != "test.xml" and not name.endswith("/"):
                zf.writestr(name, files[name])
    return {"input": str(src_path), "output": str(output), "validation": validate_test(str(output))}


def _question_summary(q: ET.Element, section_path: str, include_answers: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "tag": q.get("tag"),
        "type": q.get("type"),
        "enabled": as_bool(q.get("enabled"), True),
        "weight": q.get("weight"),
        "section": section_path,
        "images": sorted({img.get("src", "") for img in q.findall(".//img") if img.get("src")}),
        "formulaCount": len(q.findall(".//formula")),
        "hasScenario": bool(extract_script(q)),
    }
    if not include_answers:
        return result
    qtype = q.get("type")
    if qtype == "select":
        result["choices"] = [content_text(c.find("content")).strip() for c in q.findall("./choices/choice")]
        result["correct"] = [content_text(c.find("content")).strip() for c in q.findall("./choices/choice") if c.get("correct") == "true"]
    elif qtype == "input":
        result["patterns"] = [p.attrib for p in q.findall("./patterns/pattern")]
    elif qtype == "match":
        result["pairs"] = [{"base": content_text(p.find("baseItem/content")).strip(), "match": content_text(p.find("matchingItem/content")).strip()} for p in q.findall("./pairs/pair")]
        result["distractors"] = [content_text(d.find("content")).strip() for d in q.findall("./distractors/distractor")]
    elif qtype == "order":
        result["sequence"] = [content_text(i.find("content")).strip() for i in q.findall("./sequence/sequenceItem")]
        result["distractors"] = [content_text(d.find("content")).strip() for d in q.findall("./distractors/distractor")]
    elif qtype == "classify":
        result["categories"] = [{"title": content_text(c.find("categoryTitle/content")).strip(), "items": [content_text(i.find("content")).strip() for i in c.findall("./categoryItems/categoryItem")]} for c in q.findall("./categories/category")]
    return result


def _section_summary(section: ET.Element, prefix: str, include_answers: bool) -> Dict[str, Any]:
    title = section.get("title", "")
    path = "%s / %s" % (prefix, title) if prefix else title
    return {
        "title": title,
        "path": path,
        "questions": [_question_summary(q, path, include_answers) for q in section.findall("./questions/question")],
        "sections": [_section_summary(child, path, include_answers) for child in _section_children(section)],
        "questionCount": len(section.findall(".//question")),
    }


def test_info(source: str, include_answers: bool = False) -> Dict[str, Any]:
    """Return a compact authoring-oriented inventory of an .itx test."""
    path, xml_bytes, files = read_itx(source)
    root = parse_xml(xml_bytes)
    questions = list(all_question_nodes(root))
    images = sorted(n for n in files if safe_image_path(n))
    source_data = sorted(n for n in files if safe_data_path(n))
    profiles: List[Dict[str, Any]] = []
    for profile in root.findall("./profiles/profile"):
        item: Dict[str, Any] = {"title": profile.get("title", "")}
        for name in ("questionSelection", "sessionOptions", "instantFeedback", "testResults", "markScale", "sectionProfile"):
            node = profile.find(name)
            if node is not None:
                item[name] = {"attributes": dict(node.attrib), "children": len(list(node))}
        profiles.append(item)
    result: Dict[str, Any] = {
        "path": str(path),
        "version": root.get("version"),
        "questionCount": len(questions),
        "questionTypes": {t: sum(1 for q in questions if q.get("type") == t) for t in sorted(QUESTION_TYPES)},
        "tags": sorted(q.get("tag") for q in questions if q.get("tag") is not None),
        "images": images,
        "sourceData": source_data,
        "imageReferences": sorted({img.get("src", "") for img in root.findall(".//img") if img.get("src")}),
        "formulaCount": len(root.findall(".//formula")),
        "scenarioCount": sum(1 for q in questions if extract_script(q)),
        "profiles": profiles,
        "sections": [_section_summary(section, "", include_answers) for section in root.findall("./section")],
    }
    if include_answers:
        result["questions"] = [_question_summary(q, "", True) for q in questions]
    return result


def unpack_test(source: str, destination: Optional[str] = None, overwrite: bool = False) -> Dict[str, Any]:
    """Safely extract the validated test.xml and referenced image/data members."""
    path, _, files = read_itx(source)
    out = absolute_path(destination) if destination else path.with_suffix("")
    if out.exists() and (out.is_symlink() or not out.is_dir()) :
        raise ItxError("Каталог распаковки уже существует и небезопасен: %s" % out)
    if out.exists() and not overwrite:
        raise ItxError("Каталог распаковки уже существует; выберите другой путь или overwrite=true: %s" % out)
    if any(p.is_symlink() for p in out.parents):
        raise ItxError("Каталог распаковки находится через символическую ссылку")
    out.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    for name, data in files.items():
        safe = safe_archive_path(name)
        if not safe:
            raise ItxError("Небезопасное имя member: %s" % name)
        target = out.joinpath(*safe.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        checked_write_path(target, overwrite=True)
        target.write_bytes(data)
        written.append(safe)
    return {"source": str(path), "output": str(out), "files": sorted(written), "fileCount": len(written)}


def open_in_iren(source: str) -> Dict[str, Any]:
    """Validate and open an .itx through the OS file association on Windows."""
    path = absolute_path(source)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ItxError("Открытие через символическую ссылку запрещено")
    if path.suffix.lower() != ".itx" or not path.is_file():
        raise ItxError("Ожидается существующий файл .itx")
    report = validate_test(str(path))
    if not report["valid"]:
        raise ItxError("Айрен не будет надёжно открывать невалидный тест; сначала исправьте validate_test: %s" % json.dumps(report["findings"], ensure_ascii=False))
    if os.name != "nt":
        return {"opened": False, "supported": False, "path": str(path), "message": "Автоматическое открытие через ассоциацию доступно только в Windows."}
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        return {"opened": False, "supported": False, "path": str(path), "message": "В этой среде отсутствует Windows os.startfile."}
    startfile(str(path))
    return {"opened": True, "supported": True, "path": str(path), "validation": report}


def render_html(source: str, output_dir: Optional[str] = None, overwrite: bool = False) -> Dict[str, Any]:
    path, xml_bytes, files = read_itx(source)
    root = parse_xml(xml_bytes)
    out = absolute_path(output_dir) if output_dir else path.with_name(path.stem + "_html")
    if out.is_symlink() or any(p.is_symlink() for p in out.parents):
        raise ItxError("Каталог предпросмотра через символическую ссылку запрещён")
    out.mkdir(parents=True, exist_ok=True)
    checked_write_path(out / "index.html", overwrite=overwrite)
    for name, data in files.items():
        if name.startswith("images/") and not name.endswith("/"):
            safe = safe_image_path(name)
            if safe:
                target = out / Path(safe)
                target.parent.mkdir(parents=True, exist_ok=True)
                checked_write_path(target, overwrite=overwrite)
                target.write_bytes(data)
    cards: List[str] = []
    for index, q in enumerate(all_question_nodes(root), 1):
        qtype = q.get("type", "")
        loc = "Вопрос %s (tag %s, %s)" % (index, q.get("tag", "?"), qtype)
        body = ['<article class="question"><h2>%s</h2><div class="prompt">%s</div>' % (html.escape(loc), content_html(q.find("content")))]
        if qtype == "select":
            body.append("<ol class=choices>")
            for c in q.findall("./choices/choice"):
                classes = []
                if as_bool(c.get("correct"), False): classes.append("correct")
                if as_bool(c.get("negative"), False): classes.append("negative")
                flags = []
                if "correct" in classes: flags.append("верно")
                if "negative" in classes: flags.append("negative")
                marker = (" <span class=flag>[" + ", ".join(flags) + "]</span>") if flags else ""
                body.append('<li class="%s">%s%s</li>' % (" ".join(classes), content_html(c.find("content")), marker))
            body.append("</ol>")
        elif qtype == "input":
            body.append("<h3>Эталоны ответа</h3><ul>%s</ul>" % "".join("<li>%s</li>" % html.escape(p.get("value", "")) for p in q.findall("./patterns/pattern")))
        elif qtype == "match":
            body.append("<table><tr><th>Базовый элемент</th><th>Правильное соответствие</th></tr>")
            for p in q.findall("./pairs/pair"):
                body.append("<tr><td>%s</td><td>%s</td></tr>" % (content_html(p.find("baseItem/content")), content_html(p.find("matchingItem/content"))))
            body.append("</table>")
        elif qtype == "order":
            body.append("<h3>Правильная последовательность</h3><ol>%s</ol>" % "".join("<li>%s</li>" % content_html(i.find("content")) for i in q.findall("./sequence/sequenceItem")))
            if q.findall("./distractors/distractor"):
                body.append("<h3>Отвлекающие элементы</h3><ul>%s</ul>" % "".join("<li>%s</li>" % content_html(i.find("content")) for i in q.findall("./distractors/distractor")))
        elif qtype == "classify":
            body.append("<div class=categories>")
            for c in q.findall("./categories/category"):
                body.append("<section><h3>%s</h3><ul>%s</ul></section>" % (content_html(c.find("categoryTitle/content")), "".join("<li>%s</li>" % content_html(i.find("content")) for i in c.findall("./categoryItems/categoryItem"))))
            body.append("</div>")
        body.append("</article>")
        cards.append("\n".join(body))
    raw_title = root.find("./section").get("title", "") if root.find("./section") is not None else ""
    title = html.escape(raw_title or path.stem)
    doc = """<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>%s</title><style>
body{font:16px/1.45 system-ui,Segoe UI,sans-serif;max-width:1100px;margin:0 auto;padding:24px;background:#f5f6f8;color:#20242b}.question{background:white;border:1px solid #d9dde5;border-radius:10px;padding:20px;margin:18px 0;box-shadow:0 2px 6px #0000000d}.prompt{font-size:1.05em;margin:12px 0 18px}.prompt img{max-width:100%%;max-height:360px;object-fit:contain;display:block;margin:10px 0}.choices li.correct{background:#e6f6e9;border-left:4px solid #198754}.choices li.negative{background:#fff2f2}.choices li{padding:7px 10px;margin:5px 0;border-radius:5px}.flag{font-size:.8em;color:#146c43;font-weight:600}.formula{font-family:Georgia,serif;background:#f0f1f3;padding:0 4px;border-radius:3px}.categories{display:flex;gap:14px;flex-wrap:wrap}.categories section{border:1px solid #ddd;padding:8px 14px;min-width:220px}table{border-collapse:collapse;width:100%%}td,th{border:1px solid #ccd2dc;padding:7px;text-align:left;vertical-align:top}small{color:#667085}</style></head><body><h1>%s</h1><p><small>Статический предпросмотр: правильные ответы подсвечены для проверки автора.</small></p>%s</body></html>""" % (title, title, "\n".join(cards))
    index = out / "index.html"
    index.write_text(doc, encoding="utf-8")
    return {"source": str(path), "output": str(index), "questionCount": len(cards), "imageCount": len([n for n in files if safe_image_path(n)])}


TOOL_DEFS = [
    {"name": "create_test", "description": "Создать и упаковать новый тест Айрен .itx из JSON-спецификации.", "inputSchema": {"type": "object", "required": ["spec", "output"], "properties": {"spec": {"description": "JSON-объект или путь к JSON-файлу", "type": ["object", "string"]}, "output": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}}},
    {"name": "build_test", "description": "Собрать/перепаковать существующий .itx либо создать его из spec и проверить результат.", "inputSchema": {"type": "object", "oneOf": [{"required": ["input_itx", "output"]}, {"required": ["spec", "output"]}], "properties": {"input_itx": {"type": "string"}, "spec": {"type": ["object", "string"]}, "output": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}}},
    {"name": "test_info", "description": "Получить сводку по дереву секций, вопросам, профилям, изображениям, формулам, сценариям и при необходимости правильным ответам.", "inputSchema": {"type": "object", "required": ["itx"], "properties": {"itx": {"type": "string"}, "include_answers": {"type": "boolean", "default": False}}}},
    {"name": "unpack_test", "description": "Безопасно распаковать test.xml и связанные изображения/исходные данные из .itx.", "inputSchema": {"type": "object", "required": ["itx", "output_dir"], "properties": {"itx": {"type": "string"}, "output_dir": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}}},
    {"name": "validate_formulas", "description": "Проверить inline-формулы, эталоны ответов, маркеры $(...) и сценарии.", "inputSchema": {"type": "object", "required": ["itx"], "properties": {"itx": {"type": "string"}}}},
    {"name": "validate_test", "description": "Проверить ZIP/XML, вопросы, ответы, изображения, профили и формулы теста.", "inputSchema": {"type": "object", "required": ["itx"], "properties": {"itx": {"type": "string"}, "check_near_dups": {"type": "boolean", "default": False}}}},
    {"name": "render_html", "description": "Сгенерировать автономный HTML-предпросмотр всех вопросов и изображений.", "inputSchema": {"type": "object", "required": ["itx"], "properties": {"itx": {"type": "string"}, "output_dir": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}}},
    {"name": "open_in_iren", "description": "После проверки открыть валидный .itx через ассоциацию установленной программы Айрен в Windows.", "inputSchema": {"type": "object", "required": ["itx"], "properties": {"itx": {"type": "string"}}}},
]


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "create_test":
        return create_test(args)
    if name == "build_test":
        return build_test(args)
    if name == "test_info":
        return test_info(args["itx"], bool(args.get("include_answers", False)))
    if name == "unpack_test":
        return unpack_test(args["itx"], args.get("output_dir"), bool(args.get("overwrite", False)))
    if name == "validate_formulas":
        return validate_formulas(args["itx"])
    if name == "validate_test":
        return validate_test(args["itx"], bool(args.get("check_near_dups", False)))
    if name == "render_html":
        return render_html(args["itx"], args.get("output_dir"), bool(args.get("overwrite", False)))
    if name == "open_in_iren":
        return open_in_iren(args["itx"])
    raise ItxError("Неизвестный MCP tool: %s" % name)


def serve_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    for line in sys.stdin:
        if not line.strip():
            continue
        request_id = None
        try:
            request = json.loads(line)
            method = request.get("method")
            request_id = request.get("id")
            if method == "initialize":
                requested_version = request.get("params", {}).get("protocolVersion", "2024-11-05")
                if requested_version not in SUPPORTED_PROTOCOL_VERSIONS:
                    if request_id is not None:
                        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Unsupported MCP protocol version", "data": {"supported": sorted(SUPPORTED_PROTOCOL_VERSIONS), "requested": requested_version}}}, ensure_ascii=False) + "\n")
                        sys.stdout.flush()
                    continue
                result = {"protocolVersion": requested_version, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "iren-itx", "version": __version__}, "instructions": "Tools create, inspect, unpack, validate, preview, and optionally open Айрен .itx tests."}
            elif method == "notifications/initialized":
                continue
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOL_DEFS}
            elif method == "tools/call":
                params = request.get("params", {})
                try:
                    value = call_tool(params.get("name", ""), params.get("arguments", {}) or {})
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}], "isError": False}
                except Exception as exc:
                    result = {"content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}], "isError": True}
            else:
                if request_id is None:
                    continue
                raise ItxError("Неизвестный метод: %s" % method)
            if request_id is not None:
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            if request_id is not None:
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Айрен ITX MCP/CLI")
    sub = parser.add_subparsers(dest="command")
    for name in ("validate", "validate-formulas", "info", "unpack", "render", "create", "build", "open"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("--itx")
        command_parser.add_argument("--output")
        command_parser.add_argument("--spec")
        command_parser.add_argument("--answers", action="store_true")
        command_parser.add_argument("--check-near-dups", action="store_true")
        command_parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--itx")
    parser.add_argument("--output")
    parser.add_argument("--spec")
    parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args()
    if args.stdio or args.command is None:
        serve_stdio()
        return
    if args.command == "validate":
        result = validate_test(args.itx, args.check_near_dups)
    elif args.command == "validate-formulas":
        result = validate_formulas(args.itx)
    elif args.command == "info":
        result = test_info(args.itx, args.answers)
    elif args.command == "unpack":
        result = unpack_test(args.itx, args.output, args.overwrite)
    elif args.command == "render":
        result = render_html(args.itx, args.output, args.overwrite)
    elif args.command == "create":
        result = create_test({"spec": args.spec, "output": args.output, "overwrite": args.overwrite})
    elif args.command == "build":
        result = build_test({"input_itx": args.itx, "output": args.output, "overwrite": args.overwrite})
    else:
        result = open_in_iren(args.itx)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
