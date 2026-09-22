"""聊天、流式协议、记忆和文件交付隔离验收；不读用户配置、不调用真实 API。"""
from __future__ import annotations
import asyncio
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CARME_LOAD_ENV"] = "0"
import httpx
from PIL import Image
from fastapi import Depends, FastAPI
from pypdf import PdfWriter
from test_runtime import setup, settle
from carme.api.routes import build_router, require_token
from carme.attachments import save_file, image_block, file_path
from carme.config import Provider, ModelsConfig
from carme.llm import LLMGateway, LLMResponse, PartialStreamError, ToolCall, Usage
from carme.tools.base import ToolContext
from carme.tools.memory import RememberTool, RecallTool, SHARED_AGENT
from carme.tools.files import ReadAttachmentTool


class Chunks(httpx.AsyncByteStream):
    def __init__(self, events, *, gate=None):
        self.events, self.gate = events, gate

    async def __aiter__(self):
        for i, event in enumerate(self.events):
            yield ("data: " + (event if isinstance(event, str) else json.dumps(event)) + "\n\n").encode()
            if self.gate and i == 0:
                await self.gate.wait()
            await asyncio.sleep(0)


def openai_events(text="hello", tool=False):
    delta = {"tool_calls": [{"index": 0, "id": "call1", "function": {"name": "create_artifact", "arguments": '{"name":"result.md",'}}]} if tool else {"content": text}
    events = [{"model": "reported-version", "choices": [{"delta": delta}]}]
    if tool:
        events.append({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"content":"# Ready"}'}}]}}]})
    events.extend([{"choices": [{"delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]},
                   {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 5}}, "[DONE]"])
    return events


async def test_protocols(root):
    runtime, store, _ = setup(root, [])
    for protocol in ("openai", "openai_compatible", "openai_responses", "anthropic"):
        provider = Provider("p", protocol, "https://fixture.invalid/v1", "", "synthetic-key")
        runtime.config.models = ModelsConfig(providers={"p": provider}, tiers={"balanced": ["p/requested"]})
        gateway = LLMGateway(runtime.config)
        captured, output = [], []
        if protocol in ("openai", "openai_compatible"):
            events = openai_events(tool=True)
        elif protocol == "openai_responses":
            events = [{"type": "response.output_text.delta", "delta": "hello"},
                      {"type": "response.completed", "response": {"status": "completed", "model": "reported-version",
                       "usage": {"input_tokens": 7, "output_tokens": 5}, "output": [
                           {"type": "reasoning", "encrypted_content": "opaque-reasoning"},
                           {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
                           {"type": "function_call", "call_id": "call1", "name": "create_artifact", "arguments": '{"name":"result.md","content":"# Ready"}'}]}}]
        else:
            events = [{"type": "message_start", "message": {"model": "reported-version", "usage": {"input_tokens": 7}}},
                      {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                      {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "opaque-signature"}},
                      {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
                      {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "hello"}},
                      {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "call1", "name": "create_artifact", "input": {}}},
                      {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"name":"result.md",'}},
                      {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '"content":"# Ready"}'}},
                      {"type": "content_block_stop", "index": 2},
                      {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
                      {"type": "message_stop"}]
        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(events))
        gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async def notify(payload): output.append(dict(payload))
        messages = [{"role": "user", "content": [{"type": "text", "text": "check"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
        result = await gateway.chat(messages, on_stream=notify)
        assert result.model == "reported-version" and result.provider == "p" and result.streamed
        assert result.tool_calls[0].arguments == {"name": "result.md", "content": "# Ready"}
        assert result.usage.prompt_tokens == 7 and result.usage.completion_tokens == 5
        body = captured[0]
        assert body["stream"] and "AAAA" in json.dumps(body)
        if protocol == "anthropic":
            assert result.anthropic_content[0]["signature"] == "opaque-signature"
            assert body["messages"][0]["content"][1]["source"]["type"] == "base64"
        elif protocol == "openai_responses":
            assert result.response_items[0]["encrypted_content"] == "opaque-reasoning"
            assert body["input"][0]["content"][1]["type"] == "input_image"
        if output:
            assert output[-1]["status"] == "done"
        await gateway.aclose()
    # 中断不能静默重试，将两个供应商的正文拼接。
    runtime.config.models = ModelsConfig(providers={"p": Provider("p", "openai", "https://fixture.invalid", "", "synthetic-key")}, tiers={"balanced": ["p/requested"]})
    gateway = LLMGateway(runtime.config)
    calls, output = [], []
    def broken(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(openai_events()[:1]))
    gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    async def notify(payload): output.append(payload)
    try:
        await gateway.chat([{"role": "user", "content": "break"}], on_stream=notify)
        raise AssertionError("Truncated stream accepted")
    except PartialStreamError:
        pass
    assert len(calls) == 1 and output[-1]["status"] == "interrupted"
    await gateway.aclose()
    await runtime.shutdown(); store.close()
    print("PASS: four streaming protocols, fragmented tools, signatures, usage, images, no partial retry")


async def test_files_memory(root):
    runtime, store, gateway = setup(root, [LLMResponse(text="done", model="test-version", provider="test")])
    cid = store.create_conversation(["writer"])["id"]
    other = store.create_conversation(["outside"])["id"]
    store.remember("chief", "secret", "CHIEF_PRIVATE_SENTINEL")
    store.remember("writer", "preference", "WRITER_PRIVATE_SENTINEL")
    memory_task = store.create_task('writer', 'synthetic memory tool trace')
    store.finish_task(memory_task, 'fixture')
    ctx = ToolContext(runtime.config.agents.get("writer"), memory_task, store)
    store.memory_grant("writer","user","shared",write=True)  # M4: explicit shared-write fixture grant
    await RememberTool().run(ctx, "shared_note", "TEAM_SHARED_SENTINEL", shared=True)
    assert store.recall(SHARED_AGENT)[0]["value"] == "TEAM_SHARED_SENTINEL"
    assert "CHIEF_PRIVATE_SENTINEL" not in await RecallTool().run(ctx)
    files = [save_file(store, cid, "document.txt", b"Document verification 4927")]
    image = io.BytesIO(); Image.new("RGB", (200, 100), "red").save(image, "PNG")
    files.append(save_file(store, cid, "image.png", image.getvalue()))
    xml = b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DOCX text</w:t></w:r></w:p></w:body></w:document>'
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z: z.writestr("word/document.xml", xml)
    word = save_file(store, cid, "word.docx", archive.getvalue())
    assert store.get_attachment(word["id"])["text"] == "DOCX text"
    pdf = io.BytesIO(); writer = PdfWriter(); writer.add_blank_page(width=100, height=100); writer.write(pdf)
    blank = save_file(store, cid, "scan.pdf", pdf.getvalue())
    assert "OCR" in blank["note"]
    for name, raw in [("fake.png", b"bad"), ("run.exe", b"executable"), ("empty.txt", b""), ("huge.txt", b"x" * (10 * 1024 * 1024 + 1))]:
        try:
            save_file(store, cid, name, raw)
            raise AssertionError("Invalid upload accepted")
        except (ValueError, OSError): pass
    try: file_path(store, "../../.env"); raise AssertionError("Path traversal")
    except ValueError: pass
    result = await runtime.submit_message(cid, "Read files", "one", attachment_ids=[f["id"] for f in files])
    await settle(runtime)
    payload = json.dumps(gateway.calls[0]["messages"], ensure_ascii=False)
    assert "CHIEF_PRIVATE_SENTINEL" not in payload
    assert "WRITER_PRIVATE_SENTINEL" in payload and "TEAM_SHARED_SENTINEL" in payload
    assert "4927" in payload and "data:image/jpeg;base64" in payload
    assert not (await runtime.submit_message(cid, "Read files", "one", attachment_ids=[f["id"] for f in files]))["created"]
    try:
        await runtime.submit_message(cid, "Read files", "one", attachment_ids=[])
        raise AssertionError("Mismatched retry accepted")
    except ValueError: pass
    ctx.task_id = result["task_id"]
    foreign = save_file(store, other, "private.txt", b"FOREIGN_ATTACHMENT")
    try:
        await ReadAttachmentTool().run(ctx, foreign["id"])
        raise AssertionError("Cross-conversation attachment leak")
    except ValueError: pass
    app = FastAPI(); app.include_router(build_router(runtime.config, store, runtime), dependencies=[Depends(require_token)])
    previous_token = os.environ.get("CARME_TOKEN")
    os.environ["CARME_TOKEN"] = "synthetic-session"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://carme.test") as client:
        assert (await client.get(f"/api/conversations/{cid}/attachments/{files[0]['id']}/download")).status_code == 401
        client.headers["Authorization"] = "Bearer synthetic-session"
        assert (await client.get(f"/api/conversations/{other}/attachments/{files[0]['id']}/download")).status_code == 404
        response = await client.get(f"/api/conversations/{cid}/attachments/{files[0]['id']}/download")
        assert response.content == b"Document verification 4927" and "attachment" in response.headers["content-disposition"]
        assert (await client.delete(f"/api/conversations/{cid}/attachments/{files[0]['id']}")).status_code == 409
        assert (await client.delete(f"/api/conversations/{cid}/attachments/{word['id']}")).status_code == 200
        assert (await client.put('/api/memory/writer', json={"key": "preference", "value": "edited"})).status_code == 200
        assert (await client.delete('/api/memory/writer?key=preference')).status_code == 200
        assert not store.recall("writer", "preference") and store.recall("chief", "secret")
    if previous_token is None: os.environ.pop("CARME_TOKEN", None)
    else: os.environ["CARME_TOKEN"] = previous_token
    await runtime.shutdown(); store.close()
    print("PASS: private/shared memory, content extraction, image validation, auth, scoped access, immutable sent files, idempotency")


async def test_runtime_stream(root):
    runtime, store, _ = setup(root, [])
    runtime.config.models = ModelsConfig(providers={"p": Provider("p", "openai", "https://fixture.invalid", "", "synthetic-key")}, tiers={"balanced": ["p/requested"]})
    gateway = LLMGateway(runtime.config); runtime.gateway = gateway
    gate = asyncio.Event()
    mode, count = "stream", 0
    def handler(request):
        nonlocal count
        count += 1
        events = openai_events("visible before completion", tool=mode == "artifact" and count == 1)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(events, gate=gate if mode == "stream" else None))
    gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cid = store.create_conversation(["writer"])["id"]
    first = await runtime.submit_message(cid, "stream", "one")
    for _ in range(100):
        if any(m["status"] == "streaming" for m in store.list_conversation_messages(cid)): break
        await asyncio.sleep(.01)
    partial = store.list_conversation_messages(cid)[-1]
    assert partial["status"] == "streaming" and partial["content"] == "visible before completion"
    gate.set(); await settle(runtime)
    finished = store.list_conversation_messages(cid)[-1]
    assert finished["id"] == partial["id"] and finished["status"] == "done"
    assert finished["model"] == "reported-version" and len(store.list_conversation_messages(cid)) == 2
    gate.clear()
    second = await runtime.submit_message(cid, "cancel", "two")
    for _ in range(100):
        if any(m["status"] == "streaming" for m in store.list_conversation_messages(cid)): break
        await asyncio.sleep(.01)
    await runtime.cancel(second["task_id"]); await settle(runtime)
    assert not any(m["status"] == "streaming" for m in store.list_conversation_messages(cid))
    assert any(m["status"] == "interrupted" for m in store.list_conversation_messages(cid))
    mode, count = "artifact", 0
    runtime.config.agents.get("writer").tools = ["create_artifact"]  # Explicit conversation-scoped grant.
    await runtime.submit_message(cid, "create file", "three"); await settle(runtime)
    artifact = next(f for f in store.list_attachments(cid) if f["kind"] == "artifact")
    assert artifact["message_id"] and file_path(store, artifact["id"]).read_text() == "# Ready"
    assert count == 2 and store.get_task(first["task_id"])["tokens"] == 12
    await runtime.shutdown(); store.close()
    print("PASS: durable live streaming, stable IDs, cancellation, generated artifact round trip")


async def test_summaries(root):
    runtime, store, gateway = setup(root, [LLMResponse(text="Early preference: teal. Keep file f_example.", provider="p", model="summary", usage=Usage(10, 8)), LLMResponse(text="continued", model="new-model", provider="p")])
    cid = store.create_conversation(["writer"])["id"]
    for i in range(44): store.add_conversation_message(cid, "writer", "user" if i % 2 == 0 else "assistant", f"Old message {i} " + "x" * 50)
    await runtime.submit_message(cid, "continue with new model", "one"); await settle(runtime)
    summary = store.get_summary(cid)
    assert summary and "teal" in summary["content"] and len(store.list_conversation_messages(cid)) == 46
    prompt = json.dumps(gateway.calls[-1]["messages"])
    assert "teal" in prompt and "Old message 0 " not in prompt and "Old message 43 " in prompt
    assert gateway.calls[0]["system_extra"].startswith("你是对话归档助手")
    assert store.list_conversation_messages(cid)[-1]["model"] == "new-model"
    await runtime.shutdown(); store.close()
    print("PASS: incremental conversation summary, original history preserved, actual per-message model")


async def main(root):
    for test in (test_protocols, test_files_memory, test_runtime_stream, test_summaries):
        await test(root / test.__name__)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="carme-chat-files-") as directory:
        asyncio.run(main(Path(directory)))
