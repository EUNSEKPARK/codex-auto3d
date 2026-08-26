"""Per-job HTML/JSON report and the cross-job gallery (work/auto3d/index.html)."""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Any

from .config import Settings
from .jobs import Job, list_jobs
from .util import REPO_ROOT, human_duration, now_iso, read_json, relpath, write_json, write_text

STATUS_LABEL = {
    "completed": ("완료", "#1b8a4c"),
    "partial": ("부분 완료", "#b7791f"),
    "blocked": ("중단됨", "#c0392b"),
    "failed": ("실패", "#c0392b"),
    "running": ("진행 중", "#2b6cb0"),
    "created": ("생성됨", "#718096"),
    "interrupted": ("중단(사용자)", "#718096"),
}

CSS = """
:root { color-scheme: light; --bg:#f6f7f9; --card:#fff; --ink:#1f2933; --muted:#6b7280; --line:#e5e7eb; --accent:#2b6cb0; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 28px 20px 60px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 28px 0 10px; }
.sub { color: var(--muted); margin-bottom: 18px; }
.badge { display:inline-block; padding:2px 10px; border-radius:999px; color:#fff; font-size:12px; font-weight:600; vertical-align: middle; }
.grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
.card h3 { margin:0 0 8px; font-size:14px; color:var(--muted); font-weight:600; }
.kv { display:grid; grid-template-columns: 130px 1fr; gap: 4px 12px; font-size: 13px; }
.kv dt { color: var(--muted); }
.kv dd { margin:0; word-break: break-all; }
img.shot { width:100%; height:auto; border-radius:8px; border:1px solid var(--line); background:#fff; }
.shots { display:grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap:10px; }
.shots figure { margin:0; }
.shots figcaption { font-size:12px; color:var(--muted); text-align:center; margin-top:4px; }
table { width:100%; border-collapse: collapse; font-size:13px; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align: top; }
th { background:#f1f3f6; font-weight:600; color:#374151; }
tr:last-child td { border-bottom:none; }
pre { background:#0f172a; color:#e2e8f0; padding:12px 14px; border-radius:10px; overflow:auto; font-size:12px; white-space:pre-wrap; }
a { color: var(--accent); }
.pass { color:#1b8a4c; font-weight:600; } .fail { color:#c0392b; font-weight:600; } .na { color:var(--muted); }
.two { display:grid; grid-template-columns: 1fr 1fr; gap:14px; }
@media (max-width: 720px) { .two { grid-template-columns: 1fr; } }
.gallery { display:grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap:16px; }
.gallery .card img { width:100%; aspect-ratio:1; object-fit:cover; border-radius:8px; border:1px solid var(--line); }
.gallery .title { font-weight:600; margin:8px 0 2px; }
.gallery .meta { font-size:12px; color:var(--muted); }
.btn { display:inline-block; padding:7px 14px; border-radius:8px; background:var(--accent); color:#fff !important; text-decoration:none; font-weight:600; font-size:13px; margin-right:8px; }
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _rel_to(target: str | None, base: Path) -> str | None:
    """`target` values are repo-relative (see util.relpath); return them relative to the report
    directory so the HTML keeps working when the job folder is copied elsewhere as a whole."""
    if not target:
        return None
    path = Path(target)
    absolute = path if path.is_absolute() else REPO_ROOT / path
    try:
        return os.path.relpath(absolute, base)
    except ValueError:
        return str(absolute)


def _verdict_html(value: Any) -> str:
    if value in ("PASS", "ok", True):
        return '<span class="pass">PASS</span>'
    if value in ("FAIL", False):
        return '<span class="fail">FAIL</span>'
    if value in (None, "n/a"):
        return '<span class="na">n/a</span>'
    return _esc(value)


def build_report_data(job: Job) -> dict[str, Any]:
    state = job.state
    build = job.stage("build")
    image = job.stage("image")
    prompt_stage = job.stage("prompt")
    prompt_path = job.path("prompt", "prompt.json")
    prompt = read_json(prompt_path) if prompt_path.is_file() else {}
    capture_path = job.path("preview", "capture.json")
    capture = read_json(capture_path) if capture_path.is_file() else {}
    progress = build.get("progress") or {}
    return {
        "id": job.id,
        "generatedAt": now_iso(),
        "status": state.get("status"),
        "concept": state.get("concept"),
        "subject": state.get("subject") or prompt.get("subject_name"),
        "profile": build.get("profile") or state.get("profile"),
        "complexity": build.get("complexity") or state.get("complexity"),
        "targetPass": build.get("targetPass"),
        "outcome": build.get("outcome"),
        "stopReason": build.get("stopReason"),
        "prompt": prompt,
        "promptAuthor": prompt_stage.get("author"),
        "image": {
            "hero": image.get("hero"),
            "backend": image.get("backend"),
            "admitted": image.get("heroAdmitted"),
            "attempts": image.get("attempts") or [],
            "views": image.get("views") or {},
        },
        "progress": progress,
        "turns": build.get("turns") or [],
        "renders": build.get("renders") or [],
        "capture": {key: capture.get(key) for key in ("captures", "comparisonSheet", "previewHtml", "triangles", "gatesSummary", "consoleErrors", "passId", "factoryFunction")},
        "gates": {name: gate for name, gate in (capture.get("gates") or {}).items()},
        "artifacts": state.get("artifacts") or {},
        "usage": state.get("usage") or {},
        "errors": state.get("errors") or [],
        "elapsedSec": build.get("elapsedSec"),
        "createdAt": state.get("createdAt"),
        "settings": {key: state.get("settings", {}).get(key) for key in ("model", "quality", "image_backend", "image_model", "image_size", "sandbox", "max_review_turns", "views")},
    }


def write_job_report(job: Job, settings: Settings) -> Path:
    data = build_report_data(job)
    write_json(job.path("report.json"), data)
    report_path = job.path("report.html")
    base = job.dir

    label, color = STATUS_LABEL.get(str(data["status"]), (str(data["status"]), "#718096"))
    artifacts = data["artifacts"]
    preview_rel = _rel_to(artifacts.get("previewHtml"), base)
    hero_ref_rel = _rel_to(data["image"].get("hero"), base)
    cmp_rel = _rel_to(data["capture"].get("comparisonSheet"), base)
    usage = data["usage"]

    # --- sections
    head = f"""
<h1>{_esc(data.get('subject') or data.get('concept'))} <span class="badge" style="background:{color}">{label}</span></h1>
<div class="sub">job <code>{_esc(data['id'])}</code> · 생성 {_esc(data.get('createdAt'))} · 소요 {human_duration(data.get('elapsedSec') or 0)} · 리포트 {_esc(data['generatedAt'])}</div>
<div>
  {'<a class="btn" href="' + _esc(preview_rel) + '">3D 프리뷰 열기 (preview.html)</a>' if preview_rel else ''}
  {'<a class="btn" style="background:#4a5568" href="' + _esc(_rel_to(artifacts.get('factory'), base)) + '">TypeScript 팩토리</a>' if artifacts.get('factory') else ''}
  {'<a class="btn" style="background:#4a5568" href="' + _esc(_rel_to(artifacts.get('spec'), base)) + '">ObjectSculptSpec JSON</a>' if artifacts.get('spec') else ''}
  <a class="btn" style="background:#4a5568" href="../index.html">갤러리</a>
</div>
"""
    progress = data["progress"]
    summary_cards = f"""
<div class="grid" style="margin-top:18px">
  <div class="card"><h3>개념 (입력)</h3><div>{_esc(data.get('concept'))}</div></div>
  <div class="card"><h3>파이프라인</h3><dl class="kv">
    <dt>프로파일</dt><dd>{_esc(data.get('profile'))}</dd>
    <dt>복잡도</dt><dd>{_esc(data.get('complexity'))}</dd>
    <dt>목표 패스</dt><dd>{_esc(data.get('targetPass'))}</dd>
    <dt>완료 패스</dt><dd>{_esc(', '.join(progress.get('completedPasses') or []) or '없음')}</dd>
    <dt>최근 fidelity</dt><dd>{_esc(progress.get('latestFidelity'))}</dd>
    <dt>리뷰 횟수</dt><dd>{_esc(progress.get('reviewCount'))}</dd>
    <dt>중단 사유</dt><dd>{_esc(data.get('stopReason') or '-')}</dd>
  </dl></div>
  <div class="card"><h3>Codex 사용량</h3><dl class="kv">
    <dt>입력 토큰</dt><dd>{usage.get('input_tokens', 0):,}</dd>
    <dt>캐시 입력</dt><dd>{usage.get('cached_input_tokens', 0):,}</dd>
    <dt>출력 토큰</dt><dd>{usage.get('output_tokens', 0):,}</dd>
    <dt>턴 수</dt><dd>{len(data['turns'])}</dd>
    <dt>이미지 백엔드</dt><dd>{_esc(data['image'].get('backend'))} / {_esc(data['settings'].get('image_model'))}</dd>
    <dt>모델</dt><dd>{_esc(data['settings'].get('model') or 'Codex 기본')}</dd>
  </dl></div>
</div>
"""
    # comparison
    compare = ""
    if hero_ref_rel or cmp_rel:
        hero_render_rel = _rel_to(artifacts.get("heroRender"), base)
        compare = f"""
<h2>참조 이미지 vs 3D 렌더</h2>
<div class="two">
  <figure style="margin:0">{'<img class="shot" src="' + _esc(hero_ref_rel) + '">' if hero_ref_rel else ''}<figcaption class="sub" style="text-align:center;margin-top:6px">생성된 참조 이미지 (gpt-image)</figcaption></figure>
  <figure style="margin:0">{'<img class="shot" src="' + _esc(hero_render_rel) + '">' if hero_render_rel else '<div class="card">렌더 없음</div>'}<figcaption class="sub" style="text-align:center;margin-top:6px">Three.js 절차적 모델 (hero 뷰)</figcaption></figure>
</div>
{'<figure style="margin:14px 0 0"><img class="shot" src="' + _esc(cmp_rel) + '"><figcaption class="sub" style="text-align:center;margin-top:6px">비교 시트 (make_comparison_sheet.py)</figcaption></figure>' if cmp_rel else ''}
"""
    # turntable
    captures = data["capture"].get("captures") or {}
    shots = ""
    if captures:
        order = ["hero", "az000", "orbit-plus35", "orbit-minus35", "profile", "az090", "rear", "az270", "top", "head-hero", "head-threequarter", "hero-mapstripped"]
        figs = []
        for name in order:
            item = captures.get(name)
            if not item:
                continue
            rel = _rel_to(item.get("path"), base)
            figs.append(f'<figure><img class="shot" src="{_esc(rel)}" loading="lazy"><figcaption>{_esc(name)} · az {item.get("azimuth"):.0f}° el {item.get("elevation"):.0f}°</figcaption></figure>')
        shots = f"""
<h2>턴테이블 / 다각도 캡처 <span class="sub" style="font-size:12px">({_esc(data['capture'].get('triangles'))} triangles)</span></h2>
<div class="shots">{''.join(figs)}</div>
"""
    # gates
    gates = data["gates"]
    gate_rows = []
    for name, gate in gates.items():
        if name == "typecheck":
            verdict = _verdict_html(gate.get("ok") if gate.get("available") else None)
            detail = f"{gate.get('errorCount', 0)} errors" if gate.get("available") else "typescript 미설치"
            path = ""
        else:
            result = gate.get("result") if isinstance(gate, dict) else None
            if isinstance(result, dict):
                if "passed" in result:
                    verdict = _verdict_html(result["passed"])
                elif "selfIntersecting" in result:
                    verdict = _verdict_html(not result["selfIntersecting"])
                elif "degenerate" in result:
                    verdict = _verdict_html(not result["degenerate"])
                else:
                    verdict = '<span class="na">기록됨</span>'
                keys = [key for key in ("failures", "missingAzimuths", "interiorDifference", "insideVertexCount", "checks") if key in result]
                detail = "; ".join(f"{key}={json.dumps(result[key], ensure_ascii=False)[:160]}" for key in keys)
            else:
                verdict = '<span class="fail">ERROR</span>'
                detail = _esc((gate or {}).get("stderr", ""))[:200]
            path = _rel_to((gate or {}).get("path"), base) or ""
        link = f'<a href="{_esc(path)}">json</a>' if path else ""
        gate_rows.append(f"<tr><td>{_esc(name)}</td><td>{verdict}</td><td>{_esc(detail)}</td><td>{link}</td></tr>")
    gates_html = f"""
<h2>결정적 게이트 (마지막 렌더)</h2>
<table><thead><tr><th>게이트</th><th>판정</th><th>상세</th><th></th></tr></thead><tbody>{''.join(gate_rows) or '<tr><td colspan=4>없음</td></tr>'}</tbody></table>
""" if gate_rows else ""
    # reviews
    reviews = progress.get("reviews") or []
    review_rows = "".join(
        f"<tr><td>{index + 1}</td><td>{_esc(entry.get('passId'))}</td><td>{_esc(entry.get('action'))}</td><td>{_esc(entry.get('fidelity'))}</td><td>{_esc(entry.get('aiVisionScore'))}</td><td>{_esc(entry.get('summary'))}</td></tr>"
        for index, entry in enumerate(reviews)
    )
    reviews_html = f"""
<h2>리뷰 기록 (spec.reviewHistory)</h2>
<table><thead><tr><th>#</th><th>패스</th><th>결정</th><th>fidelity</th><th>AI vision</th><th>요약</th></tr></thead><tbody>{review_rows or '<tr><td colspan=6>기록 없음</td></tr>'}</tbody></table>
"""
    # turns
    turn_rows = "".join(
        f"<tr><td>{_esc(turn.get('index'))}</td><td>{_esc(turn.get('kind'))}</td><td>{_esc(turn.get('stage'))}</td><td>{_esc(turn.get('passId'))}</td>"
        f"<td>{human_duration(turn.get('durationSec') or 0)}</td><td>{(turn.get('usage') or {}).get('output_tokens', 0):,}</td><td>{_esc(turn.get('messageKo') or turn.get('message'))}</td></tr>"
        for turn in data["turns"]
    )
    turns_html = f"""
<h2>Codex 턴</h2>
<table><thead><tr><th>#</th><th>종류</th><th>결과</th><th>패스</th><th>소요</th><th>출력 토큰</th><th>메시지</th></tr></thead><tbody>{turn_rows or '<tr><td colspan=7>없음</td></tr>'}</tbody></table>
"""
    # history sheets
    history_dir = job.path("preview", "history")
    history_figs = []
    if history_dir.is_dir():
        for sheet in sorted(history_dir.glob("*-cmp.png")):
            history_figs.append(f'<figure><img class="shot" src="{_esc(_rel_to(relpath(sheet), base))}" loading="lazy"><figcaption>{_esc(sheet.stem.replace("-cmp", ""))}</figcaption></figure>')
    history_html = f"""
<h2>패스별 비교 시트 진행</h2>
<div class="shots" style="grid-template-columns: repeat(auto-fill, minmax(320px, 1fr))">{''.join(history_figs)}</div>
""" if history_figs else ""
    # prompt
    prompt = data["prompt"]
    prompt_html = f"""
<h2>이미지 프롬프트 <span class="sub" style="font-size:12px">(작성: {_esc(data.get('promptAuthor'))})</span></h2>
<div class="card"><dl class="kv">
  <dt>subject</dt><dd>{_esc(prompt.get('subject_name'))}</dd>
  <dt>identity</dt><dd>{_esc('; '.join(prompt.get('identity_features') or []))}</dd>
  <dt>materials</dt><dd>{_esc(', '.join(prompt.get('materials') or []))}</dd>
  <dt>notes</dt><dd>{_esc(prompt.get('notes_ko'))}</dd>
</dl></div>
<pre>{_esc(prompt.get('image_prompt'))}</pre>
"""
    errors = data["errors"]
    errors_html = f"""
<h2>오류 / 경고</h2>
<pre>{_esc(chr(10).join(f"[{e.get('at')}] {e.get('message')}" for e in errors))}</pre>
""" if errors else ""

    page = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(data.get('subject') or data['id'])} · codex_auto3d report</title><style>{CSS}</style></head>
<body><main>
{head}
{summary_cards}
{compare}
{shots}
{gates_html}
{reviews_html}
{history_html}
{turns_html}
{prompt_html}
{errors_html}
<p class="sub" style="margin-top:30px">codex-auto3d · img2threejs · report.json 에 동일 데이터가 기계가독 형식으로 저장되어 있습니다.</p>
</main></body></html>
"""
    write_text(report_path, page)
    return report_path


def write_gallery(settings: Settings) -> Path | None:
    root = settings.work_root_path
    jobs = list_jobs(root)
    if not jobs:
        return None
    cards = []
    for job in sorted(jobs, key=lambda j: str(j.state.get("createdAt")), reverse=True):
        state = job.state
        label, color = STATUS_LABEL.get(str(state.get("status")), (str(state.get("status")), "#718096"))
        artifacts = state.get("artifacts") or {}
        progress = (job.stage("build").get("progress") or {})
        thumb = artifacts.get("heroRender") or artifacts.get("hero") or job.stage("image").get("hero")
        thumb_rel = os.path.relpath(REPO_ROOT / thumb, root) if thumb else None
        report_rel = os.path.relpath(job.path("report.html"), root)
        preview_rel = os.path.relpath(REPO_ROOT / artifacts["previewHtml"], root) if artifacts.get("previewHtml") else None
        cards.append(
            f"""<div class="card">
  <a href="{_esc(report_rel)}">{'<img src="' + _esc(thumb_rel) + '" loading="lazy">' if thumb_rel else '<div style="aspect-ratio:1;background:#eee;border-radius:8px"></div>'}</a>
  <div class="title">{_esc(state.get('subject') or state.get('concept'))} <span class="badge" style="background:{color}">{label}</span></div>
  <div class="meta">{_esc(job.id)}</div>
  <div class="meta">passes: {_esc(', '.join(progress.get('completedPasses') or []) or '-')} · fidelity {_esc(progress.get('latestFidelity'))}</div>
  <div style="margin-top:8px"><a href="{_esc(report_rel)}">리포트</a>{(' · <a href="' + _esc(preview_rel) + '">3D 프리뷰</a>') if preview_rel else ''}</div>
</div>"""
        )
    page = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>codex_auto3d 갤러리</title><style>{CSS}</style></head>
<body><main>
<h1>codex_auto3d 갤러리</h1>
<div class="sub">{len(jobs)}개 작업 · 갱신 {_esc(now_iso())} · {_esc(relpath(root))}</div>
<div class="gallery">{''.join(cards)}</div>
</main></body></html>
"""
    path = root / "index.html"
    write_text(path, page)
    return path
