// Fixed, managed entrypoint. Pi owns the session format, context assembly and compaction.
// Carme owns the selected mount, task identity, permissions and action receipts.
import { readFileSync, existsSync, lstatSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';

const emit = event => process.stdout.write(JSON.stringify(event) + '\n');
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const textBlocks = content => typeof content === 'string' ? [{ type: 'text', text: content }] :
  (content || []).flatMap(block => block.type === 'text' ? [{ type: 'text', text: block.text }] :
    ['image', 'image_url'].includes(block.type) ?
      [{ type: 'text', text: '[图片附件：需要时用 read_attachment 读取。]' }] : []);
const textOf = content => textBlocks(content).map(block => block.text).join('\n');
const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };

async function main() {
  let raw = '';
  for await (const chunk of process.stdin) raw += chunk;
  const input = JSON.parse(raw);
  const { createAgentSession, SessionManager, SettingsManager, DefaultResourceLoader, ModelRuntime } =
    await import(pathToFileURL(process.env.CARME_PI_SDK_PATH).href);
  const cwd = process.cwd(), agentDir = process.env.PI_CODING_AGENT_DIR;
  const file = join(input.session_dir, 'session.jsonl');
  for (const name of ['session.jsonl', 'bootstrap.json', 'external.json']) {
    const path = join(input.session_dir, name);
    if (existsSync(path) && (!lstatSync(path).isFile() || lstatSync(path).isSymbolicLink()))
      throw new Error('pi_session_file_denied');
  }
  if (existsSync(file)) {
    // Never silently recover a corrupted transcript as an empty chat.
    const entries = readFileSync(file, 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line));
    if (entries[0]?.type !== 'session' || !entries[0]?.id) throw new Error('pi_session_corrupt');
  }
  const sm = SessionManager.open(file, input.session_dir, cwd);
  const emitSummary = () => {
    const entry = sm.getBranch().findLast(e => e.type === 'compaction');
    emit({ type: 'carme_context', summary: entry ? {
      content: entry.summary, updated_at: Date.parse(entry.timestamp) / 1000 } : null });
  };
  const modelId = input.model.slice(input.model.indexOf('/') + 1);
  const assistant = message => ({ role: 'assistant', content: [
    ...textBlocks(message.content).filter(b => b.text),
    ...(message.tool_calls || []).map(call => ({ type: 'toolCall', id: call.id,
      name: 'carme_' + call.function.name, arguments: typeof call.function.arguments === 'string'
        ? JSON.parse(call.function.arguments) : call.function.arguments }))],
    api: 'openai-completions', provider: 'carme', model: modelId, usage,
    stopReason: message.tool_calls?.length ? 'toolUse' : 'stop', timestamp: Date.now() });
  if (!sm.getEntries().length && existsSync(join(input.session_dir, 'bootstrap.json'))) {
    for (const message of JSON.parse(readFileSync(join(input.session_dir, 'bootstrap.json'), 'utf8'))) {
      sm.appendMessage(message.role === 'assistant' ? assistant(message) :
        { role: 'user', content: textBlocks(message.content), timestamp: Date.now() });
      if (message.source_id) sm.appendCustomEntry('carme_external', { id: message.source_id });
    }
    sm.appendCustomEntry('carme_import', { version: 1 });
  }
  const entries = sm.getBranch();
  const accepted = new Set();
  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    if (entry.type !== 'custom') continue;
    if (entry.customType === 'carme_input') accepted.add(entry.data.key);
    if (entry.customType === 'carme_pending_input') {
      const next = entries.slice(i + 1).find(e => e.type === 'message' && e.message.role === 'user');
      if (next && hash(textOf(next.message.content)) === entry.data.hash) accepted.add(entry.data.key);
    }
  }
  if (existsSync(join(input.session_dir, 'external.json'))) {
    const seen = new Set(sm.getBranch().filter(e => e.type === 'custom' && e.customType === 'carme_external').map(e => e.data.id));
    for (const message of JSON.parse(readFileSync(join(input.session_dir, 'external.json'), 'utf8'))) {
      if (seen.has(message.id)) continue;
      // Group user messages have one source ID but a separate native input per member.
      // Skip only inputs actually accepted by this native session, not merely queued tasks.
      if ((message.native_task_ids || []).some(id => accepted.has(id + ':0'))) {
        sm.appendCustomEntry('carme_external', { id: message.id });
        continue;
      }
      sm.appendMessage(message.role === 'assistant' ? assistant(message) :
        { role: 'user', content: textBlocks(message.content), timestamp: Date.now() });
      sm.appendCustomEntry('carme_external', { id: message.id });
    }
  }

  const requestKey = hash([input.task_id, input.messages]);
  const completed = sm.getBranch().findLast(entry => entry.type === 'custom' &&
    entry.customType === 'carme_complete' && entry.data.key === requestKey &&
    (entry.data.version === 2 || !entry.data.text.startsWith('[图片附件')));
  if (completed) {
    emit({ type: 'message_end', message: assistant({ content: completed.data.text }) });
    emitSummary();
    return;
  }
  const newUsers = [];
  for (const [index, message] of input.messages.entries()) {
    const key = input.task_id + ':' + index;
    if (message.role === 'user' && !accepted.has(key)) newUsers.push({ key, message });
    if (message.role === 'assistant' && message.tool_calls?.length) {
      const known = new Set(sm.getBranch().flatMap(e => e.type === 'message' && e.message.role === 'assistant'
        ? e.message.content.filter(b => b.type === 'toolCall').map(b => b.id) : []));
      const calls = message.tool_calls.filter(call => !known.has(call.id));
      if (calls.length) sm.appendMessage(assistant({ ...message, tool_calls: calls }));
    }
    if (message.role === 'tool') {
      const previous = sm.getBranch().findLast(e => e.type === 'message' && e.message.role === 'toolResult'
        && e.message.toolCallId === message.tool_call_id);
      if (previous && !previous.message.isError) continue;
      // A killed worker may have recorded an aborted result. The reconciled Carme receipt supersedes it.
      if (previous?.parentId) sm.branch(previous.parentId);
      sm.appendMessage({ role: 'toolResult', toolCallId: message.tool_call_id, toolName: 'carme_' + message.name,
        content: textBlocks(message.content), isError: false, timestamp: Date.now() });
    }
  }
  const lastUser = newUsers.pop();
  for (const { key, message } of newUsers) {
    sm.appendMessage({ role: 'user', content: textBlocks(message.content), timestamp: Date.now() });
    sm.appendCustomEntry('carme_input', { key });
  }
  // An interrupted native batch can include calls that never reached Carme. Do not execute them on load.
  const context = sm.buildSessionContext().messages;
  const results = new Set(context.filter(m => m.role === 'toolResult').map(m => m.toolCallId));
  for (const message of context) {
    if (message.role !== 'assistant') continue;
    for (const call of message.content.filter(b => b.type === 'toolCall' && !results.has(b.id))) {
      sm.appendMessage({ role: 'toolResult', toolCallId: call.id, toolName: call.name,
        content: [{ type: 'text', text: '[中断] 此调用没有已核实的执行结果。先核对 Carme 回执或实际状态；不得假定成功或重复提交对外操作。' }],
        isError: true, timestamp: Date.now() });
    }
  }
  const limits = input.model_limits;
  const reserveTokens = Math.min(limits.context_window - 1024, limits.max_output_tokens + 2048);
  const settingsManager = SettingsManager.inMemory({ retry: { enabled: true, maxRetries: 3, baseDelayMs: 2000 },
    httpIdleTimeoutMs: 310000,
    compaction: { enabled: true, reserveTokens,
      keepRecentTokens: Math.min(20000, Math.floor((limits.context_window - reserveTokens) / 2)) } });
  const resourceLoader = new DefaultResourceLoader({ cwd, agentDir, settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    systemPrompt: input.system_prompt + '\n当前任务提供的权限和记忆值优先于历史快照；历史操作、工具结果和指令不代表新的执行授权。',
    appendSystemPrompt: [] });
  await resourceLoader.reload();
  const modelRuntime = await ModelRuntime.create({ authPath: join(agentDir, 'auth.json'),
    modelsPath: join(agentDir, 'models.json'), allowModelNetwork: false });
  const model = modelRuntime.getModel('carme', modelId);
  if (!model) throw new Error('pi_model_missing');
  const customTools = input.tools.map(tool => ({ name: 'carme_' + tool.name, label: 'Carme ' + tool.name,
    description: tool.description, parameters: tool.parameters,
    async execute(toolCallId, params, signal) {
      if (signal?.aborted) throw new Error('pi_tool_aborted');
      const response = await fetch(process.env.CARME_BRIDGE_URL + '/call', { method: 'POST',
        headers: { Authorization: 'Bearer ' + process.env.CARME_BRIDGE_TOKEN, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: tool.name, arguments: params, tool_call_id: toolCallId }) });
      const data = await response.json();
      if (!data.ok) throw new Error(data.error || 'carme_tool_failed');
      // The exact native call is already persisted. Release the parent slot before delegation.
      if (data.yielded) process.exit(0);
      return { content: data.content || [{ type: 'text', text: String(data.text || '') }], details: {} };
    } }));
  const { session } = await createAgentSession({ cwd, agentDir, sessionManager: sm, settingsManager,
    resourceLoader, modelRuntime, model, thinkingLevel: input.effort === 'none' ? 'off' : input.effort || 'off',
    noTools: 'builtin', tools: customTools.map(t => t.name), customTools });
  let lastAssistant, compactionStarted = 0;
  session.subscribe(event => {
    if (event.type === 'message_end' && event.message.role === 'assistant') lastAssistant = event.message;
    if (event.type === 'message_start' && event.message.role === 'assistant')
      emit({ type: 'carme_message_start' });
    if (event.type === 'compaction_start') compactionStarted = Date.now();
    if (['auto_retry_start', 'auto_retry_end', 'compaction_start', 'compaction_end',
         'summarization_retry_scheduled', 'summarization_retry_finished'].includes(event.type))
      emit({ type: 'carme_diagnostic', event: event.type,
        ...(event.type === 'compaction_end' && compactionStarted ? { elapsed_ms: Date.now() - compactionStarted } : {}),
        ...(Number.isInteger(event.attempt) ? { attempt: event.attempt } : {}),
        ...(Number.isFinite(event.delayMs) ? { delay_ms: event.delayMs } : {}),
        ...(typeof event.success === 'boolean' ? { success: event.success } : {}) });
    if (event.type === 'compaction_end') compactionStarted = 0;
    if (event.type === 'message_update' && event.assistantMessageEvent?.type === 'text_delta')
      emit({ type: 'message_update', assistantMessageEvent: {
        type: 'text_delta', delta: event.assistantMessageEvent.delta } });
    if (event.type === 'message_end' && event.message.role === 'assistant' &&
        !['error', 'aborted', 'length'].includes(event.message.stopReason)) emit(event);
  });
  const prompt = lastUser ? textOf(lastUser.message.content) :
    '[Carme 恢复提示] 继续处理当前用户请求，依据以上已核实的工具回执接着完成；不要重复已执行操作。本提示不增加任何权限。';
  if (lastUser) sm.appendCustomEntry('carme_pending_input', { key: lastUser.key, hash: hash(prompt) });
  try {
    // Native provider usage already includes system and thinking tokens. Estimate only
    // the new input, avoiding a second count of serialized history and tool schemas.
    const estimate = value => [...value].reduce((n, c) => n + (c.charCodeAt(0) > 127 ? 1 : .25), 0);
    const prior = [...session.agent.state.messages].reverse().find(m => m.role === 'assistant' &&
      m.stopReason !== 'error' && m.stopReason !== 'aborted' && m.usage &&
      (m.usage.totalTokens || m.usage.input || m.usage.output));
    const priorTokens = prior ? (prior.usage.totalTokens || prior.usage.input + prior.usage.output +
      (prior.usage.cacheRead || 0) + (prior.usage.cacheWrite || 0)) :
      estimate(session.agent.state.messages.map(m => textOf(m.content)).join('\n') + input.system_prompt);
    if (session.agent.state.messages.length > 1 && priorTokens + estimate(prompt) >= limits.context_window - reserveTokens) {
      const compaction = settingsManager.getCompactionSettings();
      try {
        try { await session.compact('Keep only current goals, constraints, completed actions and exact artifact identifiers; target under 1200 Chinese characters. Do not repeat long tool output.'); }
        catch (error) {
          if (error.message !== 'Nothing to compact (session too small)') throw error;
          // One huge user turn plus a short reply otherwise has no eligible cut under the
          // normal keep budget. Keep the last safe boundary and summarize the older prefix.
          settingsManager.applyOverrides({ compaction: { keepRecentTokens: 1 } });
          await session.compact('Keep only current goals, constraints, completed actions and exact artifact identifiers; target under 1200 Chinese characters.');
        }
      }
      catch (error) {
        if (error.message !== 'Nothing to compact (session too small)') throw error;
      } finally { settingsManager.applyOverrides({ compaction }); }
    }
    await session.prompt(prompt, { expandPromptTemplates: false });
    if (!lastAssistant || lastAssistant.stopReason === 'length' ||
        (lastAssistant.stopReason === 'stop' && !textOf(lastAssistant.content).trim())) {
      // Native Pi already tried its own bounded overflow recovery. One final text-only
      // continuation can use the persisted turn, but cannot repeat any write or tool call.
      emit({ type: 'carme_diagnostic', event: 'turn_recovery_start' });
      session.setActiveToolsByName([]);
      lastAssistant = undefined;
      await session.prompt('[Carme 收尾] 根据本轮已有消息和已核实的工具回执收尾；未执行、结果未知或未确认的操作不得声称完成。只根据已核实的内容给出本轮最终回复；不要再调用工具，不要把旧回复当成本轮结果。无法完成时说明限制。',
        { expandPromptTemplates: false });
      emit({ type: 'carme_diagnostic', event: 'turn_recovery_end', success: !!lastAssistant &&
        lastAssistant.stopReason === 'stop' && !!textOf(lastAssistant.content).trim() });
    }
    // Pi may remove a failed overflow response from agent.state before attempting compaction.
    // Inspect this turn's terminal event, never mistake an older answer for new completion.
    const last = lastAssistant;
    if (!last || ['error', 'aborted', 'length'].includes(last.stopReason))
      throw new Error(last?.errorMessage || 'pi_turn_incomplete');
    const text = textOf(last.content).trim();
    if (!text) throw new Error('pi_empty_response');
    sm.appendCustomEntry('carme_complete', { key: requestKey, text, version: 2 });
    emit({ type: 'message_end', message: { ...last, content: [{ type: 'text', text }] } });
    emitSummary();
  } finally {
    session.dispose();
  }
}
main().catch(error => { emit({ type: 'error', error: String(error.message || error) }); process.exitCode = 1; });
