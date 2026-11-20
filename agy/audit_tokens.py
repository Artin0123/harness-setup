"""Audit script for agy CLI: captures outgoing payloads, counts exact tokens via
Gemini :countTokens, runs A/B differential probing (p1.txt vs p2.txt) via stream-json,
and compares expected vs returned tokens & hidden content.
"""

import argparse
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from typing import Any


def find_agy_exe() -> str:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        p = os.path.join(local_app_data, "agy", "bin", "agy.exe")
        if os.path.isfile(p):
            return p
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        p = os.path.join(user_profile, ".gemini", "bin", "agy.exe")
        if os.path.isfile(p):
            return p
    w = shutil.which("agy")
    if w:
        return w
    return "agy"


def dump_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def extract_xml_tags(text: str) -> list[dict[str, Any]]:
    """Find top-level XML-like <tag>...</tag> blocks in text."""
    pattern = re.compile(r"<([A-Za-z0-9_:-]+)>(.*?)</\1>", re.DOTALL)
    return [{"tag": m.group(1), "chars": len(m.group(0))} for m in pattern.finditer(text)]


class CaptureServerHandler(http.server.BaseHTTPRequestHandler):
    captures: list[dict[str, Any]] = []
    lock = threading.Lock()
    upstream_base: str = ""
    api_key: str = ""

    def log_message(self, format: str, *args) -> None:
        pass

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.upstream_base:
            self._forward_get()
            return
        if "/models" in self.path:
            self._send_json(
                200,
                {
                    "models": [
                        {
                            "name": "models/gemini-3.8-flash",
                            "displayName": "Gemini 3.8 Flash",
                            "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
                        }
                    ]
                },
            )
            return
        self._send_json(200, {"status": "ok"})

    def _forward_get(self) -> None:
        url = self.upstream_base.rstrip("/") + self.path
        headers = {"x-goog-api-key": self.api_key} if self.api_key else {}
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = {"raw": raw.decode("utf-8", errors="replace")}

        record: dict[str, Any] = {
            "method": "POST",
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items() if k.lower() not in ("x-goog-api-key", "authorization")},
            "body": parsed,
            "response": None,
        }

        if self.upstream_base:
            url = self.upstream_base.rstrip("/") + self.path
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["x-goog-api-key"] = self.api_key
            req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    resp_bytes = resp.read()
                    ct = resp.headers.get("Content-Type", "application/json")
                    self.send_response(resp.status)
                    self.send_header("Content-Type", ct)
                    self.send_header("Content-Length", str(len(resp_bytes)))
                    self.end_headers()
                    self.wfile.write(resp_bytes)
                    record["response"] = resp_bytes.decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                err_bytes = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_bytes)))
                self.end_headers()
                self.wfile.write(err_bytes)
                record["response"] = err_bytes.decode("utf-8", errors="replace")
        else:
            response_obj = {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "CAPTURED_OK"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 12,
                },
            }
            if "alt=sse" in self.path or "streamGenerateContent" in self.path:
                sse = f"data: {json.dumps(response_obj)}\n\n".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(sse)))
                self.end_headers()
                self.wfile.write(sse)
            else:
                self._send_json(200, response_obj)

        with CaptureServerHandler.lock:
            CaptureServerHandler.captures.append(record)


def analyze_payload(body: dict[str, Any], raw_prompt: str) -> dict[str, Any]:
    sys_inst = body.get("systemInstruction")
    tools = body.get("tools", [])
    contents = body.get("contents", [])
    gen_cfg = body.get("generationConfig")
    extra_keys = [k for k in body.keys() if k not in ("systemInstruction", "tools", "contents", "generationConfig")]

    sys_str = dump_compact(sys_inst) if sys_inst is not None else ""
    tools_str = dump_compact(tools) if tools else ""
    contents_str = dump_compact(contents) if contents else ""
    gen_str = dump_compact(gen_cfg) if gen_cfg is not None else ""
    total_str = dump_compact(body)

    sys_texts = []
    if isinstance(sys_inst, dict):
        for p in sys_inst.get("parts", []):
            if isinstance(p, dict) and "text" in p:
                sys_texts.append(p["text"])
    sys_plain = "\n".join(sys_texts)

    tool_items = []
    for t in tools if isinstance(tools, list) else []:
        if isinstance(t, dict) and "functionDeclarations" in t:
            for fn in t["functionDeclarations"]:
                tool_items.append({"name": fn.get("name", "<unknown>"), "chars": len(dump_compact(fn))})
        else:
            tool_items.append(
                {
                    "name": ",".join(t.keys()) if isinstance(t, dict) else "unknown",
                    "chars": len(dump_compact(t)),
                }
            )

    content_texts = []
    for c in contents if isinstance(contents, list) else []:
        if isinstance(c, dict):
            for p in c.get("parts", []):
                if isinstance(p, dict) and "text" in p:
                    content_texts.append(p["text"])
    content_plain = "\n".join(content_texts)

    return {
        "top_level_keys": list(body.keys()),
        "extra_keys": extra_keys,
        "raw_prompt_chars": len(raw_prompt),
        "sections": {
            "systemInstruction": {
                "chars": len(sys_str),
                "xml_blocks": extract_xml_tags(sys_plain),
            },
            "tools": {
                "chars": len(tools_str),
                "tool_count": len(tool_items),
                "declarations": tool_items,
            },
            "contents": {
                "chars": len(contents_str),
                "injected_tags": extract_xml_tags(content_plain),
                "preview": content_plain[:500],
            },
            "generationConfig": {
                "chars": len(gen_str),
                "value": gen_cfg,
            },
        },
        "total_payload_chars": len(total_str),
        "total_payload_bytes": len(total_str.encode("utf-8")),
    }


def gemini_count_tokens(api_key: str, model: str, payload: dict[str, Any]) -> int:
    """Call official Gemini :countTokens endpoint (required)."""
    clean_model = model
    for suffix in ("-low", "-medium", "-high", "-xhigh"):
        if clean_model.endswith(suffix):
            clean_model = clean_model[: -len(suffix)]
            break

    inner: dict[str, Any] = {}
    for k in ("contents", "systemInstruction", "tools"):
        if k in payload and payload[k]:
            inner[k] = payload[k]
    if "contents" not in inner:
        inner["contents"] = [{"role": "user", "parts": [{"text": " "}]}]

    candidates = (clean_model, "gemini-2.5-flash") if "gemini" in clean_model.lower() else ("gemini-2.5-flash",)
    last_err = ""
    for candidate in candidates:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{candidate}:countTokens"
        req_body = {"generateContentRequest": {"model": f"models/{candidate}", **inner}}
        data = json.dumps(req_body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if "totalTokens" in res:
                    return int(res["totalTokens"])
                last_err = f"Missing totalTokens in response ({candidate})"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} ({candidate}): {e.read().decode('utf-8', errors='replace')[:200]}"
        except Exception as e:
            last_err = f"{type(e).__name__} ({candidate}): {e}"

    raise RuntimeError(f"Gemini :countTokens 呼叫失敗: {last_err}")


def parse_stream_json(raw_out: str) -> dict[str, Any]:
    """Parse NDJSON stream-json output into init, per-step usages, and final result."""
    init_event = None
    step_usages: list[dict[str, Any]] = []
    result_event = None

    for line in raw_out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        etype = ev.get("event")
        if etype == "init":
            init_event = ev.get("init")
        elif etype == "step_update":
            su = ev.get("step_update", {})
            if su.get("state") == "DONE" and isinstance(su.get("usage"), dict):
                step_usages.append(
                    {
                        "step_index": su.get("step_index"),
                        "step_type": su.get("step_type"),
                        "duration_seconds": su.get("duration_seconds"),
                        "usage": su.get("usage"),
                    }
                )
        elif etype == "result":
            result_event = ev.get("result")

    return {"init": init_event, "step_usages": step_usages, "result": result_event}


def run_capture_pass(
    agy_exe: str,
    model: str,
    agent: str,
    prompt: str,
    timeout_s: int,
    extra_settings: dict[str, Any] | None,
    disable_slash_commands: bool,
    proxy_upstream: str = "",
    api_key: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, int, str]:
    CaptureServerHandler.captures = []
    CaptureServerHandler.upstream_base = proxy_upstream
    CaptureServerHandler.api_key = api_key

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CaptureServerHandler)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    settings_obj = dict(extra_settings or {})
    settings_obj["modelProvider"] = "gemini"

    cmd = [
        agy_exe,
        f"--settings={json.dumps(settings_obj)}",
        f"--model={model}",
        "--output-format=stream-json",
        f"--print-timeout={timeout_s}s",
        f"-p={prompt}",
    ]
    if agent:
        cmd.append(f"--agent={agent}")
    if disable_slash_commands:
        cmd.append("--disable-slash-commands")

    env = os.environ.copy()
    env["GEMINI_API_KEY"] = api_key if (proxy_upstream and api_key) else "dummy-audit-key"
    env["GOOGLE_GEMINI_BASE_URL"] = base_url

    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    captures = list(CaptureServerHandler.captures)
    server.shutdown()
    server.server_close()

    stream_parsed = parse_stream_json(proc.stdout) if proxy_upstream else None
    return captures, stream_parsed, proc.returncode, proc.stderr.strip()


def run_real_pass(
    agy_exe: str,
    model: str,
    agent: str,
    prompt: str,
    timeout_s: int,
    extra_settings: dict[str, Any] | None,
    disable_slash_commands: bool,
) -> tuple[dict[str, Any], int, str]:
    cmd = [
        agy_exe,
        f"--model={model}",
        "--output-format=stream-json",
        f"--print-timeout={timeout_s}s",
        f"-p={prompt}",
    ]
    if extra_settings:
        cmd.append(f"--settings={json.dumps(extra_settings)}")
    if agent:
        cmd.append(f"--agent={agent}")
    if disable_slash_commands:
        cmd.append("--disable-slash-commands")

    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    stream_parsed = parse_stream_json(proc.stdout.strip())
    return stream_parsed, proc.returncode, proc.stderr.strip()


def read_transcript(conversation_id: str) -> list[dict[str, Any]]:
    user_profile = os.environ.get("USERPROFILE", "")
    if not user_profile or not conversation_id:
        return []
    path = os.path.join(
        user_profile,
        ".gemini",
        "antigravity-cli",
        "brain",
        conversation_id,
        ".system_generated",
        "logs",
        "transcript.jsonl",
    )
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def run_single_audit(
    agy_exe: str,
    model: str,
    agent: str,
    prompt: str,
    timeout_s: int,
    extra_settings: dict[str, Any] | None,
    disable_slash_commands: bool,
    skip_real: bool,
    api_key: str,
    proxy_upstream: str,
) -> dict[str, Any]:
    captures, proxy_stream, cap_rc, cap_err = run_capture_pass(
        agy_exe=agy_exe,
        model=model,
        agent=agent,
        prompt=prompt,
        timeout_s=timeout_s,
        extra_settings=extra_settings,
        disable_slash_commands=disable_slash_commands,
        proxy_upstream=proxy_upstream,
        api_key=api_key,
    )
    if not captures:
        raise RuntimeError(f"Failed to capture local agy request (rc={cap_rc}): {cap_err}")

    first_body = captures[0]["body"]
    payload_analysis = analyze_payload(first_body, prompt)

    total_exact = gemini_count_tokens(api_key, model, first_body)
    c_only = gemini_count_tokens(api_key, model, {"contents": first_body.get("contents", [])})
    sys_plus_c = (
        gemini_count_tokens(
            api_key,
            model,
            {"contents": first_body.get("contents", []), "systemInstruction": first_body.get("systemInstruction")},
        )
        if first_body.get("systemInstruction")
        else c_only
    )
    tools_plus_c = (
        gemini_count_tokens(
            api_key,
            model,
            {"contents": first_body.get("contents", []), "tools": first_body.get("tools")},
        )
        if first_body.get("tools")
        else c_only
    )
    official_counts = {
        "total": total_exact,
        "contents": c_only,
        "systemInstruction": max(0, sys_plus_c - c_only),
        "tools": max(0, tools_plus_c - c_only),
    }

    real_run = None
    transcript: list[dict[str, Any]] = []
    comparison: dict[str, Any] = {"findings": []}

    stream_data = proxy_stream if proxy_upstream else None
    if not skip_real and not proxy_upstream:
        stream_data, r_rc, r_err = run_real_pass(
            agy_exe=agy_exe,
            model=model,
            agent=agent,
            prompt=prompt,
            timeout_s=timeout_s,
            extra_settings=extra_settings,
            disable_slash_commands=disable_slash_commands,
        )
        real_run = {"returncode": r_rc, "stream": stream_data, "stderr": r_err}
    elif proxy_upstream:
        real_run = {"returncode": cap_rc, "stream": proxy_stream, "stderr": cap_err, "via_proxy": True}

    if stream_data and stream_data.get("result"):
        res = stream_data["result"]
        conv_id = res.get("conversation_id")
        if conv_id:
            transcript = read_transcript(conv_id)

        step_usages = stream_data.get("step_usages", [])
        if not step_usages and transcript:
            for s in transcript:
                if s.get("type") == "PLANNER_RESPONSE" and isinstance(s.get("input_tokens"), int):
                    step_usages.append(
                        {
                            "step_index": s.get("step_index"),
                            "step_type": "transcript_fallback",
                            "duration_seconds": None,
                            "usage": {
                                "input_tokens": s.get("input_tokens", 0),
                                "output_tokens": s.get("output_tokens", 0),
                                "thinking_tokens": s.get("thinking_tokens", 0),
                                "cache_read_tokens": s.get("cache_read_tokens", 0),
                            },
                        }
                    )
            if step_usages:
                stream_data["step_usages"] = step_usages

        turn1_usage = step_usages[0]["usage"] if step_usages else res.get("usage", {})
        total_usage = res.get("usage", {})
        turn1_in = turn1_usage.get("input_tokens")
        total_in = total_usage.get("input_tokens") or sum(su["usage"].get("input_tokens", 0) for su in step_usages)

        if isinstance(turn1_in, int) and turn1_in > 0:
            diff_off = turn1_in - total_exact
            comparison["turn1_input_tokens"] = turn1_in
            comparison["total_input_tokens"] = total_in
            comparison["diff_vs_official"] = diff_off
            comparison["chars_per_actual_token"] = payload_analysis["total_payload_chars"] / turn1_in

            if abs(diff_off) > 100:
                comparison["findings"].append(
                    f"Official countTokens ({total_exact:,}) differs from Turn 1 input_tokens ({turn1_in:,}) by {diff_off:+,} tokens — possible server-side prompt/context injection!"
                )

            init_tools = (stream_data.get("init") or {}).get("tools", [])
            sent_tools = payload_analysis["sections"]["tools"]["tool_count"]
            if init_tools and len(init_tools) != sent_tools:
                comparison["findings"].append(
                    f"Tool count mismatch: init reported {len(init_tools)} tools, but payload sent {sent_tools} functionDeclarations."
                )

            if payload_analysis["sections"]["contents"]["injected_tags"]:
                tags = [t["tag"] for t in payload_analysis["sections"]["contents"]["injected_tags"]]
                comparison["findings"].append(f"User prompt wrapped in contents with tags: {tags}")

            if payload_analysis["sections"]["systemInstruction"]["chars"] > 1000:
                comparison["findings"].append(
                    f"systemInstruction size: {payload_analysis['sections']['systemInstruction']['chars']:,} chars ({official_counts['systemInstruction']:,} tok)."
                )

            if sent_tools > 0:
                comparison["findings"].append(
                    f"{sent_tools} tool schemas attached ({payload_analysis['sections']['tools']['chars']:,} chars, {official_counts['tools']:,} tok)."
                )

            thinking = total_usage.get("thinking_tokens", 0)
            if thinking:
                comparison["findings"].append(f"Hidden thinking_tokens consumed: {thinking:,}")

            if len(step_usages) > 1:
                comparison["findings"].append(
                    f"Multi-turn execution ({len(step_usages)} steps): Turn 1 input={turn1_in:,}, Total accumulated input={total_in:,}."
                )

            if transcript:
                user_steps = [s for s in transcript if s.get("type") == "USER_INPUT"]
                if user_steps:
                    t_content = user_steps[0].get("content", "")
                    c_preview = payload_analysis["sections"]["contents"]["preview"]
                    if t_content[:100] != c_preview[:100]:
                        comparison["findings"].append(
                            "Transcript USER_INPUT differs from locally captured contents[0]!"
                        )

    return {
        "config": {
            "model": model,
            "agent": agent,
            "prompt": prompt,
            "disable_slash_commands": disable_slash_commands,
            "capture_count": len(captures),
            "proxy_upstream": bool(proxy_upstream),
        },
        "payload_analysis": payload_analysis,
        "official_count_tokens": official_counts,
        "real_run": real_run,
        "transcript": transcript,
        "comparison": comparison,
        "captures": captures,
    }


def print_report(
    report: dict[str, Any],
    title: str = "AGY TOKEN & HIDDEN CONTENT AUDIT REPORT",
    suppress_findings: set[str] | None = None,
    brief_payload: bool = False,
) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)
    print(f"Model : {report['config']['model']}")
    print(f"Agent : {report['config']['agent'] or '(default)'}")
    raw_p = report["config"]["prompt"]
    p_preview = raw_p if len(raw_p) <= 120 else raw_p[:117] + "..."
    print(f"Prompt: {p_preview!r} ({report['payload_analysis']['raw_prompt_chars']} chars)")
    print()

    pa = report["payload_analysis"]
    sec = pa["sections"]
    oc = report["official_count_tokens"]
    print("[1] LOCAL CAPTURED PAYLOAD BREAKDOWN (Turn 1 what agy sends)")
    print(f"  - contents                   : {sec['contents']['chars']:,} chars | {oc['contents']:,} tokens")
    if sec["contents"]["injected_tags"]:
        tags_str = ", ".join(f"<{t['tag']}>({t['chars']}c)" for t in sec["contents"]["injected_tags"])
        print(f"      Injected tags in contents: {tags_str}")
    print(f"  - systemInstruction          : {sec['systemInstruction']['chars']:,} chars | {oc['systemInstruction']:,} tokens")
    if not brief_payload and sec["systemInstruction"]["xml_blocks"]:
        top_blocks = sorted(sec["systemInstruction"]["xml_blocks"], key=lambda x: x["chars"], reverse=True)[:8]
        for b in top_blocks:
            print(f"      <{b['tag']}>: {b['chars']:,} chars")
    print(f"  - tools ({sec['tools']['tool_count']:2d} declarations)     : {sec['tools']['chars']:,} chars | {oc['tools']:,} tokens")
    if not brief_payload:
        print(f"  - generationConfig           : {sec['generationConfig']['chars']:,} chars")
    if pa["extra_keys"]:
        print(f"  - EXTRA TOP-LEVEL KEYS       : {pa['extra_keys']}")
    print(f"  => Total captured JSON       : {pa['total_payload_chars']:,} chars ({pa['total_payload_bytes']:,} bytes)")
    print(f"  => Gemini countTokens (exact): {oc['total']:,} tokens")
    print()

    real = report.get("real_run")
    stream = real.get("stream") if real else None
    if stream and stream.get("result"):
        rj = stream["result"]
        step_usages = stream.get("step_usages", [])
        usage = rj.get("usage", {})
        init_ev = stream.get("init") or {}
        print("[2] REAL BACKEND RESPONSE & PER-TURN USAGE (stream-json)")
        print(f"  - Status          : {rj.get('status')} (turns={rj.get('num_turns')}, {rj.get('duration_seconds', 0):.2f}s)")
        print(f"  - Conversation ID : {rj.get('conversation_id')}")
        if init_ev:
            print(f"  - Init tools list : {len(init_ev.get('tools', []))} tools")
        for su in step_usages:
            u = su["usage"]
            print(
                f"  - Step {su['step_index']} ({su['step_type']}): in={u.get('input_tokens', 0):,}, out={u.get('output_tokens', 0):,}, think={u.get('thinking_tokens', 0):,}, cache={u.get('cache_read_tokens', 0):,}"
            )
        tot_in = usage.get("input_tokens") or sum(su["usage"].get("input_tokens", 0) for su in step_usages)
        tot_out = usage.get("output_tokens") or sum(su["usage"].get("output_tokens", 0) for su in step_usages)
        tot_think = usage.get("thinking_tokens") or sum(su["usage"].get("thinking_tokens", 0) for su in step_usages)
        tot_all = usage.get("total_tokens") or (tot_in + tot_out + tot_think)
        print(f"  - Total usage     : in={tot_in:,}, out={tot_out:,}, think={tot_think:,}, total={tot_all:,}")
        resp_text = rj.get("response", "")
        print(f"  - Visible response: {len(resp_text)} chars -> {resp_text.strip()[:120]!r}")
        print()

        comp = report.get("comparison", {})
        turn1_in = comp.get("turn1_input_tokens")
        print("[3] TURN-1 COMPARISON & HIDDEN CONTENT CHECK")
        if turn1_in is not None:
            print(
                f"  - Expected (official) vs Turn-1 input_tokens: {oc['total']:,} vs {turn1_in:,} (diff={comp.get('diff_vs_official'):+,})"
            )
            print(f"  - Effective chars/token ratio               : {comp.get('chars_per_actual_token'):.2f} chars/token")
        for flag in comp.get("findings", []):
            if suppress_findings and flag in suppress_findings:
                continue
            print(f"  [!] {flag}")
    else:
        print("[2] REAL BACKEND RUN SKIPPED OR FAILED")
        if real and real.get("stderr"):
            print(f"  STDERR: {real['stderr']}")
    print("=" * 72)


def print_diff_summary(base: dict[str, Any], probe: dict[str, Any]) -> None:
    b_off = base["official_count_tokens"]["total"]
    p_off = probe["official_count_tokens"]["total"]
    d_off = p_off - b_off

    b_in = base["comparison"].get("turn1_input_tokens")
    p_in = probe["comparison"].get("turn1_input_tokens")

    print()
    print("=" * 72)
    print("A/B DIFFERENTIAL PROBE SUMMARY (cancels static system/tool overhead)")
    print("=" * 72)
    print(f"  - Baseline official     : {b_off:,} tok | Probe official     : {p_off:,} tok | Δ_official = {d_off:+,} tok")
    if b_in is not None and p_in is not None:
        d_act = p_in - b_in
        unexplained = d_act - d_off
        print(f"  - Baseline Turn-1 actual: {b_in:,} tok | Probe Turn-1 actual: {p_in:,} tok | Δ_actual   = {d_act:+,} tok")
        print(f"  - Unexplained delta (Δ_actual - Δ_official): {unexplained:+,} tok")
        if abs(unexplained) > 20:
            print("  [!] Significant unexplained delta vs official countTokens! Probe likely triggered extra hidden context.")
        else:
            print("  [OK] Delta matches official countTokens cleanly.")
    print("=" * 72)


def resolve_prompt_file(path: str) -> str:
    """Resolve prompt file against CWD first, then fallback to script directory."""
    if os.path.isfile(path):
        return path
    if not os.path.isabs(path):
        script_rel = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        if os.path.isfile(script_rel):
            return script_rel
    raise FileNotFoundError(
        f"找不到提示詞檔案: {path!r}（目前工作目錄: {os.getcwd()}，腳本目錄: {os.path.dirname(os.path.abspath(__file__))}）"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit agy outgoing payload vs Gemini :countTokens and real backend usage (A/B file mode)"
    )
    parser.add_argument("--model", default="gemini-3.8-flash-low")
    parser.add_argument("--agent", default="build", help="Agent name (default: build; pass '' for default agent)")
    parser.add_argument("--prompt-file", default="p1.txt", help="Baseline UTF-8 .txt file (default: p1.txt)")
    parser.add_argument("--probe-prompt-file", default="p2.txt", help="Probe UTF-8 .txt file (default: p2.txt)")
    parser.add_argument("--settings", default="", help="Optional JSON string for --settings")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--disable-slash-commands", action="store_true")
    parser.add_argument("--skip-real", action="store_true", help="Only run local capture & :countTokens, skip real agy call")
    parser.add_argument("--gemini-api-key", default="", help="Gemini API key (required unless GEMINI_API_KEY env var is set)")
    parser.add_argument(
        "--proxy-upstream",
        default="",
        help="Optional upstream Gemini base URL (e.g. https://generativelanguage.googleapis.com) for single-pass exact capture",
    )
    parser.add_argument("--out-dir", default="", help="Optional directory to save audit_report.json and captured_request.json")
    args = parser.parse_args()

    api_key = args.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key or api_key == "dummy-audit-key":
        print("[ERROR] 必須提供 Gemini API Key（請傳入 --gemini-api-key 或設定 GEMINI_API_KEY 環境變數）。", file=sys.stderr)
        return 1

    try:
        with open(resolve_prompt_file(args.prompt_file), encoding="utf-8-sig") as f:
            prompt_text = f.read()
        with open(resolve_prompt_file(args.probe_prompt_file), encoding="utf-8-sig") as f:
            probe_prompt_text = f.read()

        if not prompt_text.strip() or not probe_prompt_text.strip():
            print("[ERROR] 基準檔或探針檔內容為空，請確認兩個 .txt 檔案皆有內容。", file=sys.stderr)
            return 1

        extra_settings = json.loads(args.settings) if args.settings else None
        agy_exe = find_agy_exe()

        report = run_single_audit(
            agy_exe=agy_exe,
            model=args.model,
            agent=args.agent,
            prompt=prompt_text,
            timeout_s=args.timeout,
            extra_settings=extra_settings,
            disable_slash_commands=args.disable_slash_commands,
            skip_real=args.skip_real,
            api_key=api_key,
            proxy_upstream=args.proxy_upstream,
        )
        print_report(report, title="AGY BASELINE REPORT")

        probe_report = run_single_audit(
            agy_exe=agy_exe,
            model=args.model,
            agent=args.agent,
            prompt=probe_prompt_text,
            timeout_s=args.timeout,
            extra_settings=extra_settings,
            disable_slash_commands=args.disable_slash_commands,
            skip_real=args.skip_real,
            api_key=api_key,
            proxy_upstream=args.proxy_upstream,
        )
        print_report(
            probe_report,
            title="AGY PROBE REPORT",
            suppress_findings=set(report["comparison"].get("findings", [])),
            brief_payload=True,
        )
        print_diff_summary(report, probe_report)
    except (FileNotFoundError, json.JSONDecodeError, RuntimeError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        clean_base = {k: v for k, v in report.items() if k != "captures"}
        clean_probe = {k: v for k, v in probe_report.items() if k != "captures"}
        with open(os.path.join(args.out_dir, "audit_report.json"), "w", encoding="utf-8") as f:
            json.dump({"baseline": clean_base, "probe": clean_probe}, f, indent=2, ensure_ascii=False)
        with open(os.path.join(args.out_dir, "captured_request.json"), "w", encoding="utf-8") as f:
            json.dump({"baseline": report["captures"], "probe": probe_report["captures"]}, f, indent=2, ensure_ascii=False)
        print(f"\nSaved audit_report.json and captured_request.json to: {args.out_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
