#!/usr/bin/env python3
"""TalkTime pipeline — 영어 회화 수업 스크립트 → 구조화된 학습 포스트.

input/script.md 코드블록에 붙여넣은 수업 스크립트 전체를 하나의 항목으로 읽어,
Claude로 분석해 다음 섹션으로 구성된 영문 학습 포스트를 생성한다:
  - Idioms (설명 + 예문 2개)
  - Vocabulary to Remember (단어 설명 + 예문)
  - Say It Better (수업 중 틀리기 쉬운 말 vs 교정 문장)
  - Check Yourself (토글 아코디언 퀴즈)
  - Mini Diary (이디엄을 활용한 짧은 일기 문장)

코드블록 안에서 `---` 만 있는 줄로 구분하면 스크립트 여러 개를 각각 별도 포스트로
처리한다. 이미 게시에 사용된 스크립트(텍스트 해시 기준)는 다시 나타나도 건너뛴다.
입력이 비어 있으면 FALLBACK_QUOTES(클래식 이디엄 풀)에서 그날의 항목을 대신 사용한다.

Usage:
    python pipeline/generate.py [--dry-run]

Env:
    JUDGE_BACKEND            "claude-code" | "api" (기본: 자동 — claude CLI가 있으면
                             claude-code, 없으면 api)
    CLAUDE_CODE_OAUTH_TOKEN  claude-code 백엔드 CI 인증 (claude setup-token으로 발급,
                             로컬은 claude 로그인 세션 사용)
    ANTHROPIC_API_KEY        api 백엔드 필수
    CLAUDE_MODEL             생성 모델 (기본 claude-sonnet-4-6)
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SENTENCE_FILE = ROOT / "input" / "script.md"
STATE_FILE = ROOT / "pipeline" / "state.json"
CONTENT_DIR = ROOT / "content" / "posts"

KST = timezone(timedelta(hours=9))

# ============================== 도메인 설정 =================================
# 이 블록만 새 프로젝트 주제에 맞게 교체한다. 아래 엔진 코드는 건드릴 필요 없다.

# 후킹 전용 모드: 입력이 비어 있으면 아무것도 게시하지 않는다.
# 매일 이디엄 미니 레슨을 자동 게시하려면 크론 트리거를 되살리고 이 풀을 채운다
# (예: {"text": "break the ice", "author": ""}).
FALLBACK_QUOTES = []

# Claude에게 부여할 역할/톤
SYSTEM_PROMPT = """You are an experienced ESL conversation coach. You analyze raw \
transcripts from English conversation classes (imperfect speech-to-text output with \
fillers, mishearings, and mixed speakers is expected) and turn them into concise, \
encouraging study notes for intermediate learners. All output is in natural English. \
When correcting learner sentences, quote what was actually said in the transcript and \
keep the speaker's intended meaning. Never invent quotes that are not grounded in the \
transcript. This output is published on a public website, so you must never carry over \
any personally identifying or private information from the transcript — see the privacy \
rules below."""

# {sentence} / {note} 두 자리를 반드시 유지. JSON 스키마의 이중 중괄호는 str.format()
# 이스케이프이므로 스키마를 고칠 때도 그대로 유지한다.
GENERATE_PROMPT = """Below is a raw transcript from an English conversation class \
(speech-to-text output; it contains fillers, transcription errors, and multiple \
speakers).{note} Analyze it and produce study notes. Respond ONLY with JSON in exactly \
this format, no other text:

{{"title": "Short English title capturing the session's main topic",
 "summary": "2-3 sentence English overview of what was discussed in the session",
 "idioms": [
   {{"idiom": "idiom or expression used or worth learning from this session",
     "meaning": "plain-English explanation",
     "examples": ["example sentence 1", "example sentence 2"]}}
 ],
 "vocabulary": [
   {{"word": "word or phrase worth remembering", "meaning": "plain-English definition",
     "example": "one natural example sentence"}}
 ],
 "corrections": [
   {{"original": "sentence (or close paraphrase) a learner actually said in the transcript",
     "corrected": "natural, correct version of the same sentence",
     "note": "one short line on why / the key pattern"}}
 ],
 "quiz": [
   {{"question": "natural sentence containing ____ (a blank) where one studied word or idiom fits",
     "options": ["studied word/idiom A", "studied word/idiom B", "studied word/idiom C"],
     "answer": "studied word/idiom B",
     "explanation": "one short line on why it fits the blank and the others don't"}}
 ],
 "diary": "one short first-person diary entry (4-6 sentences) told as a single connected story",
 "tags": ["kebab-case-tag", "max 3"]}}

Privacy rules (apply to every field — summary, corrections, quiz, diary): strip or \
generalize anything that could identify a real person from the transcript. Never \
include real names, nicknames, or initials (rewrite as "a participant", "one learner", \
"a classmate", etc.); never include exact ages, specific medical/health conditions or \
diagnoses, immigration/visa status, specific employers or job titles, school/university \
names, exact home addresses or neighborhoods, phone numbers, emails, social-media \
handles, or other contact/financial details, even if the transcript states them plainly. \
Keep only what serves the English lesson: the language pattern, the general topic/theme, \
and enough everyday context to make examples natural (e.g. "a recent trip" instead of \
naming the city and travel companion if those aren't needed for the language point). \
When in doubt, generalize rather than omit the language teaching value.

Requirements: 2-4 idioms (each with exactly 2 examples), 4-8 vocabulary items,
5-10 corrections drawn from what learners actually said (correct the language only —
do not attribute corrections to a named speaker).
Diary rules: the diary is ONE coherent entry about ONE small everyday event
(e.g. walking past an old neighborhood, a dinner invitation) — not a list of
disconnected sentences. Tell it as a natural mini-story with a beginning and
end, weaving in 2-4 of the studied idioms/vocabulary only where they genuinely
fit; wrap each studied expression in **double asterisks** so it stands out.
Never force in more expressions at the cost of natural flow.
Quiz rules (4-6 questions): every question is FILL-IN-THE-BLANK — a natural
sentence with "____" marking the blank. Every option (3-4 per question) MUST be
taken verbatim from this lesson's "idioms" or "vocabulary" entries — never invent
outside words, so all distractors are plausible items the learner just studied.
Exactly one option fits the blank; the sentence must be fully grammatical when
the correct option is inserted (adjust the option's form — tense, plural,
agreement — inside the option text if needed). The same option set should not
repeat across questions. Do not copy an example sentence from the
idioms/vocabulary sections as a quiz sentence — write a fresh sentence.

Transcript:
{sentence}"""

QUOTE_NOTE = (
    " Actually, there is no class transcript today. Instead, build a mini lesson"
    " anchored on this classic English idiom: treat it as the first item in \"idioms\","
    " add 1-2 related expressions, use typical mistakes intermediate ESL learners make"
    " with it for \"corrections\", and base the quiz and the diary entry on it."
)

# 포스트 본문 섹션 제목
HEADING_INPUT = "Session Overview"
HEADING_INPUT_QUOTE = "Today's Idiom"
HEADING_IDIOMS = "💬 Idioms"
HEADING_VOCAB = "📚 Vocabulary to Remember"
HEADING_CORRECTIONS = "🔧 Say It Better"
HEADING_QUIZ = "✅ Check Yourself"
HEADING_DIARY = "✍️ Mini Diary"

# ============================ 도메인 설정 끝 =================================


def log(msg: str) -> None:
    print(msg, flush=True)


def sentence_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    return (slug or "talktime")[:60].rstrip("-")


def read_sentences() -> list[str]:
    """input/script.md 코드블록 안의 스크립트를 읽는다.

    코드블록 전체가 항목 하나. `---` 만 있는 줄로 구분하면 여러 스크립트를
    각각 별도 항목(= 별도 포스트)으로 처리한다.
    """
    if not SENTENCE_FILE.exists():
        log(f"오류: {SENTENCE_FILE} 파일이 없습니다")
        sys.exit(1)
    text = SENTENCE_FILE.read_text(encoding="utf-8")
    fenced = re.search(r"```[a-zA-Z]*\n(.*?)```", text, re.DOTALL)
    body = fenced.group(1) if fenced else text
    scripts = []
    for chunk in re.split(r"^\s*---+\s*$", body, flags=re.MULTILINE):
        chunk = chunk.strip()
        if chunk and not chunk.startswith("<!--"):
            scripts.append(chunk)
    return scripts


def fallback_quote_item(today) -> dict | None:
    """input이 비어 있을 때 사용할 항목 — 날짜 기준으로 풀을 순환 선택."""
    if not FALLBACK_QUOTES:
        return None
    idx = today.timetuple().tm_yday % len(FALLBACK_QUOTES)
    quote = FALLBACK_QUOTES[idx]
    return {
        "text": quote["text"],
        "source": quote.get("author") or "idiom",
        # 날짜를 해시에 포함 — 같은 항목이 몇 주 뒤 다시 나와도 새로 게시되도록
        "dedup_key": sentence_hash(f"{today.isoformat()}::{quote['text']}"),
    }


def build_queue(sentences: list[str], today) -> list[dict]:
    if sentences:
        return [
            {"text": s, "source": None, "dedup_key": sentence_hash(s)}
            for s in sentences
        ]
    fallback = fallback_quote_item(today)
    return [fallback] if fallback else []


class FatalAPIError(Exception):
    """재시도가 무의미한 오류(크레딧 부족, 인증 실패) — 실행 전체 중단."""


def is_fatal_api_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in (
        "credit balance", "authenticat", "invalid x-api-key",
        "invalid api key", "invalid bearer token", "oauth token", "/login",
        "401",
    ))


def parse_result(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    required = ("title", "summary")
    if not all(isinstance(data.get(k), str) and data.get(k) for k in required):
        return None
    for key in ("idioms", "vocabulary", "corrections", "quiz"):
        value = data.get(key) or []
        data[key] = value if isinstance(value, list) else []
    diary = data.get("diary") or ""
    if isinstance(diary, list):  # 모델이 옛 형식(문장 목록)으로 답한 경우 이어붙임
        diary = " ".join(str(d).strip() for d in diary)
    data["diary"] = str(diary).strip()
    if not data["idioms"]:
        return None
    tags = data.get("tags") or []
    data["tags"] = [slugify(str(t)) for t in tags[:3] if str(t).strip()] or ["talktime"]
    return data


def build_prompt(sentence: str, source: str | None) -> str:
    note = QUOTE_NOTE if source else ""
    return GENERATE_PROMPT.format(sentence=sentence, note=note)


def generate_api(client, model: str, sentence: str, source: str | None = None) -> dict | None:
    prompt = build_prompt(sentence, source)
    for attempt in (1, 2):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            if is_fatal_api_error(exc):
                raise FatalAPIError(str(exc)) from exc
            log(f"  API 오류 (시도 {attempt}): {exc}")
            if attempt == 2:
                return None
            continue
        text = next((b.text for b in response.content if b.type == "text"), "")
        result = parse_result(text)
        if result:
            return result
        log(f"  JSON 파싱 실패 (시도 {attempt}): {text[:120]!r}")
    return None


def generate_cli(model: str, sentence: str, source: str | None = None) -> dict | None:
    prompt = build_prompt(sentence, source)
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    cmd = ["claude", "-p", "--model", model, "--tools", "",
           "--output-format", "text", "--append-system-prompt", SYSTEM_PROMPT]
    for attempt in (1, 2):
        try:
            result = subprocess.run(cmd, input=prompt, env=env, timeout=360,
                                     capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            log(f"  CLI 타임아웃 (시도 {attempt})")
            continue
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip()
            if is_fatal_api_error(RuntimeError(err)):
                raise FatalAPIError(err[:300])
            log(f"  CLI 오류 (시도 {attempt}): {err[:200]}")
            if attempt == 2:
                return None
            continue
        parsed = parse_result(result.stdout)
        if parsed:
            return parsed
        log(f"  JSON 파싱 실패 (시도 {attempt}): {result.stdout[:120]!r}")
    return None


def yaml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_post(sentence: str, result: dict, date: datetime, source: str | None = None) -> Path:
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    base = f"{date.date().isoformat()}-{slugify(result['title'])}"
    path = CONTENT_DIR / f"{base}.md"
    n = 2
    while path.exists():
        path = CONTENT_DIR / f"{base}-{n}.md"
        n += 1

    tags = list(result["tags"])
    if source:
        tags = (tags + ["idiom-of-the-day"])[:4]
    tags_str = ", ".join(yaml_quote(t) for t in tags)

    sections = []

    if source:
        sections.append(f"## {HEADING_INPUT_QUOTE}\n\n> **{sentence}**\n\n{result['summary']}\n")
    else:
        sections.append(f"## {HEADING_INPUT}\n\n{result['summary']}\n")

    if result["idioms"]:
        lines = [f"## {HEADING_IDIOMS}\n"]
        for item in result["idioms"]:
            lines.append(f"### “{item.get('idiom', '')}”\n")
            lines.append(f"{item.get('meaning', '')}\n")
            examples = item.get("examples") or []
            if examples:
                lines.append("\n".join(f"- *{ex}*" for ex in examples) + "\n")
        sections.append("\n".join(lines))

    if result["vocabulary"]:
        lines = [f"## {HEADING_VOCAB}\n"]
        for item in result["vocabulary"]:
            word = item.get("word", "")
            meaning = item.get("meaning", "")
            example = item.get("example", "")
            entry = f"- **{word}** — {meaning}"
            if example:
                entry += f"\n  - *{example}*"
            lines.append(entry)
        sections.append("\n".join(lines) + "\n")

    if result["corrections"]:
        lines = [f"## {HEADING_CORRECTIONS}\n"]
        for i, item in enumerate(result["corrections"], 1):
            lines.append(f"{i}. ❌ *{item.get('original', '')}*")
            lines.append(f"   ✅ **{item.get('corrected', '')}**")
            note = item.get("note", "")
            if note:
                lines.append(f"   💡 {note}")
        sections.append("\n".join(lines) + "\n")

    if result["quiz"]:
        lines = [f"## {HEADING_QUIZ}\n"]
        for i, item in enumerate(result["quiz"], 1):
            lines.append(f"**Q{i}.** {item.get('question', '')}\n")
            options = item.get("options") or []
            if options:
                lines.append("\n".join(f"- {opt}" for opt in options) + "\n")
            answer = html_escape(str(item.get("answer", "")))
            explanation = html_escape(str(item.get("explanation", "")))
            detail = f"<strong>{answer}</strong>"
            if explanation:
                detail += f" — {explanation}"
            lines.append(
                "<details><summary>Show answer</summary>"
                f"<p>{detail}</p></details>\n"
            )
        sections.append("\n".join(lines))

    if result["diary"]:
        entry = result["diary"].replace("\n", "\n> ")
        sections.append(f"## {HEADING_DIARY}\n\n> {entry}\n")

    post = f"""---
title: {yaml_quote(f"{date.date().isoformat()} {result['title']}")}
date: {date.isoformat()}
tags: [{tags_str}]
---
""" + "\n".join(sections)
    path.write_text(post, encoding="utf-8")
    return path


def clear_input() -> None:
    """게시가 끝난 뒤 input/script.md 코드블록을 비운다 (안내 주석은 유지)."""
    text = SENTENCE_FILE.read_text(encoding="utf-8")
    cleared = re.sub(r"```[a-zA-Z]*\n.*?```", "```\n```", text, count=1, flags=re.DOTALL)
    if cleared != text:
        SENTENCE_FILE.write_text(cleared, encoding="utf-8")
        log("input/script.md 코드블록을 비웠습니다 (게시 완료)")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="TalkTime pipeline")
    parser.add_argument("--dry-run", action="store_true",
                         help="파일 생성/state.json 갱신 없이 결과만 출력")
    args = parser.parse_args()

    backend = os.environ.get("JUDGE_BACKEND", "").strip() or (
        "claude-code" if shutil.which("claude") else "api"
    )
    client = None
    if backend == "api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log("오류: api 백엔드에는 ANTHROPIC_API_KEY 환경변수가 필요합니다")
            return 1
        import anthropic  # 지연 임포트

        client = anthropic.Anthropic()
    elif backend == "claude-code":
        if not shutil.which("claude"):
            log("오류: claude-code 백엔드에는 claude CLI가 PATH에 있어야 합니다")
            return 1
    else:
        log(f"오류: 알 수 없는 JUDGE_BACKEND={backend!r} (claude-code | api)")
        return 1

    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    today = datetime.now(KST).date()
    sentences = read_sentences()
    queue = build_queue(sentences, today)
    if sentences:
        log(f"입력된 스크립트 {len(sentences)}개")
    elif queue:
        log(f"input/script.md 에 스크립트가 없어 이디엄으로 대체합니다: {queue[0]['text']}")
    else:
        log("input/script.md 에 스크립트가 없고 FALLBACK_QUOTES도 비어 있어 오늘은 건너뜁니다")
        return 0

    state = load_state()
    processed: dict = state.get("processed", {})

    log(f"=== 생성 시작 (backend={backend}, model={model}, dry_run={args.dry_run}) ===")

    new_count = 0
    skipped_dup = 0
    failed = 0
    fatal_error = None
    for item in queue:
        sentence, source, h = item["text"], item["source"], item["dedup_key"]
        if h in processed:
            skipped_dup += 1
            continue

        preview = sentence if len(sentence) <= 80 else sentence[:80] + "…"
        log(f"\n오늘의 항목 ({len(sentence)}자): {preview}")
        try:
            if backend == "claude-code":
                result = generate_cli(model, sentence, source)
            else:
                result = generate_api(client, model, sentence, source)
        except FatalAPIError as exc:
            fatal_error = exc
            break

        if result is None:
            log("  생성 실패 — 건너뜁니다 (다음 실행에서 재시도)")
            failed += 1
            continue

        now = datetime.now(KST)
        log(f"  → {result['title']}")

        if args.dry_run:
            log(json.dumps(result, ensure_ascii=False, indent=2))
            continue

        path = write_post(sentence, result, now, source)
        log(f"  생성 파일: {path.relative_to(ROOT)}")
        processed[h] = now.date().isoformat()
        new_count += 1

    log(f"\n=== 결과: 신규 {new_count} / 중복 스킵 {skipped_dup} / 생성 실패 {failed} ===")

    if args.dry_run:
        log("(dry-run — 파일 생성/기록 갱신 없음)")
        return 1 if fatal_error else 0

    if new_count:
        state["processed"] = processed
        STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")

    # 전부 성공했을 때만 입력란 초기화 — 실패분이 있으면 다음 실행 재시도를 위해 남겨둔다
    if sentences and new_count and not failed and fatal_error is None:
        clear_input()

    if fatal_error:
        log(f"\n중단: 복구 불가능한 API 오류 — {fatal_error}")
        log("→ Anthropic 크레딧/API 키(또는 CLAUDE_CODE_OAUTH_TOKEN)를 확인하세요.")
        log("→ 성공한 항목은 이미 게시/기록되었습니다.")
        return 1
    return 1 if failed and not new_count else 0


if __name__ == "__main__":
    sys.exit(main())
