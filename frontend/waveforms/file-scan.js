import {strictWaveformJSON} from './strict-json.js';

const MAX_FILE = 33554432 + 513, MAX_I64 = 9223372036854775807n, MIN_I64 = -9223372036854775808n;
const COMMON = 'schema kind gateway_id device_id capture_id';
const fail = (code = 'contract_invalid') => {throw Error(code);};
export async function digest(bytes) {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
}
function fields(value, names) {
  const keys = names.split(' ');
  if (!value || Array.isArray(value) || typeof value !== 'object' || Object.keys(value).length !== keys.length || keys.some(k => !Object.hasOwn(value, k))) fail();
}
function match(value, pattern) {if (typeof value !== 'string' || !pattern.test(value)) fail(); return value;}
const name = v => match(v, /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/);
const uuid = v => match(v, /^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$/);
const sha = v => match(v, /^[0-9a-f]{64}$/);
const optionalUUID = v => v === null ? v : uuid(v);
function text(v, max = 128) {if (typeof v !== 'string' || [...v].length < 1 || [...v].length > max || /[\p{Cc}\p{Cs}]/u.test(v)) fail();}
function choice(v, list) {if (!list.split(' ').includes(v)) fail();}
function count(v, min = 0, max = 2147483647) {if (typeof v !== 'number' || !Number.isInteger(v) || v < min || v > max) fail(); return v;}
function i64(v, nonnegative = false) {
  match(v, /^(?:0|-?[1-9][0-9]*)$/); if (v.replace('-', '').length > 19) fail();
  const n = BigInt(v); if (n < (nonnegative ? 0n : MIN_I64) || n > MAX_I64) fail(); return n;
}
function utc(v) {
  match(v, /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$/);
  const d = new Date(v); if (!Number.isFinite(d.getTime()) || d.toISOString().slice(0, 19) !== v.slice(0, 19) || v.startsWith('0000')) fail();
}
function gcd(a, b) {while (b) [a, b] = [b, a % b]; return a;}
function channel(c) {
  fields(c, 'channel_id quantity unit encoding measurement_source signal_kind config_id calibration_id');
  name(c.channel_id); text(c.quantity); text(c.unit, 32); choice(c.encoding, 'i32le f32le');
  choice(c.measurement_source, 'sensor derived synthetic unknown'); choice(c.signal_kind, 'instantaneous rms window_mean derived');
  if (c.config_id !== null) name(c.config_id); optionalUUID(c.calibration_id);
}
function stream(s) {
  fields(s, 'stream_id source clock_domain_id rate_num rate_den first_sample_index offset_ns frame_count frame_bytes chunk_frames chunk_count decoded_bytes data_sha256 channels');
  name(s.stream_id); fields(s.source, 'gateway_id device_id boot_id'); name(s.source.gateway_id); name(s.source.device_id); uuid(s.source.boot_id); name(s.clock_domain_id);
  count(s.rate_num, 1); count(s.rate_den, 1, 1000000); const first = i64(s.first_sample_index, true), offset = i64(s.offset_ns);
  for (const k of ['frame_count', 'frame_bytes', 'chunk_frames', 'chunk_count', 'decoded_bytes']) count(s[k], 1);
  sha(s.data_sha256); if (!Array.isArray(s.channels) || !s.channels.length || s.channels.length > 32) fail('layout_invalid');
  s.channels.forEach(channel);
  if (new Set(s.channels.map(c => c.channel_id)).size !== s.channels.length || gcd(s.rate_num, s.rate_den) !== 1 || s.rate_num < s.rate_den || s.rate_num > 128000 * s.rate_den ||
    s.frame_bytes !== 5 * s.channels.length || s.decoded_bytes !== s.frame_count * s.frame_bytes || s.chunk_count !== Math.ceil(s.frame_count / s.chunk_frames) ||
    s.chunk_frames * s.frame_bytes > 131072 || first + BigInt(s.frame_count) - 1n > MAX_I64) fail('layout_invalid');
  const last = offset * BigInt(s.rate_num) + BigInt(s.frame_count - 1) * 1000000000n * BigInt(s.rate_den);
  if (last < MIN_I64 * BigInt(s.rate_num) || last > MAX_I64 * BigInt(s.rate_num)) fail('layout_invalid');
  return [[offset, 1n], [last, BigInt(s.rate_num)]];
}
function manifest(m, length) {
  fields(m, COMMON + ' boot_id capture_kind source_kind producer source_evidence utc_start utc_uncertainty_ns clock_domain_id sync_evidence_id window_ns cycle streams');
  uuid(m.boot_id); choice(m.capture_kind, 'cycle event'); choice(m.source_kind, 'synthetic bench_measurement field_measurement unknown');
  fields(m.producer, 'manufacturer recorder_version'); text(m.producer.manufacturer); text(m.producer.recorder_version);
  optionalUUID(m.source_evidence); optionalUUID(m.sync_evidence_id); name(m.clock_domain_id);
  if (m.utc_start !== null) utc(m.utc_start); if (m.utc_uncertainty_ns !== null) i64(m.utc_uncertainty_ns, true);
  const window = i64(m.window_ns, true); if (!window || window > 60000000000n) fail();
  if (!Array.isArray(m.streams) || m.streams.length < 1 || m.streams.length > 8) fail('layout_invalid');
  const times = [[0n, 1n], ...m.streams.flatMap(stream)];
  if (m.cycle === null) {if (m.capture_kind === 'cycle') fail();}
  else {
    const c = m.cycle; fields(c, 'cycle_id start_ns turn_ns end_ns direction zero_reference reference_channel reference_events'); uuid(c.cycle_id);
    const start = i64(c.start_ns), turn = i64(c.turn_ns), end = i64(c.end_ns);
    if (!(start < turn && turn < end)) fail('layout_invalid'); times.push([start, 1n], [turn, 1n], [end, 1n]);
    choice(c.direction, 'clockwise counterclockwise unknown'); text(c.zero_reference); name(c.reference_channel);
    if (!Array.isArray(c.reference_events) || c.reference_events.length !== 3) fail();
    c.reference_events.forEach((e, i) => {
      fields(e, 'stream_id sample_index event'); choice(e.event, ['start', 'turn', 'end'][i]); name(e.stream_id);
      const s = m.streams.find(s => s.stream_id === e.stream_id && s.channels.some(ch => ch.channel_id === c.reference_channel));
      if (!s) fail('layout_invalid');
      if (e.sample_index !== null) {const index = i64(e.sample_index, true); if (index < BigInt(s.first_sample_index) || index >= BigInt(s.first_sample_index) + BigInt(s.frame_count)) fail('layout_invalid');}
    });
  }
  times.sort((a, b) => a[0] * b[1] < b[0] * a[1] ? -1 : a[0] * b[1] > b[0] * a[1] ? 1 : 0);
  const lo = times[0], hi = times.at(-1);
  if (hi[0] * lo[1] - lo[0] * hi[1] > window * hi[1] * lo[1] || new Set(m.streams.map(s => s.stream_id)).size !== m.streams.length ||
    m.streams.reduce((n, s) => n + s.channels.length, 0) > 32 || m.streams.reduce((n, s) => n + s.decoded_bytes, 0) > 16777216 ||
    m.streams.reduce((n, s) => n + s.chunk_count, 0) > 512 || length + m.streams.reduce((n, s) => n + Math.ceil(s.decoded_bytes / 3) * 4, 0) > 33554432) fail('layout_invalid');
}
async function chunk(c) {
  fields(c, COMMON + ' stream_id chunk_index sample_start sample_count manifest_sha256 data_sha256 data_base64');
  name(c.stream_id); count(c.chunk_index); count(c.sample_start); count(c.sample_count, 1); sha(c.manifest_sha256); sha(c.data_sha256);
  if (typeof c.data_base64 !== 'string' || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(c.data_base64)) fail();
  const decoded = atob(c.data_base64); if (btoa(decoded) !== c.data_base64) fail();
  const bytes = Uint8Array.from(decoded, c => c.charCodeAt(0));
  if (bytes.length > 131072) fail('payload_too_large');
  if (!bytes.length || bytes.length % (c.sample_count * 5) || c.sample_start + c.sample_count > 2147483647) fail('layout_invalid');
  for (let p = 0; p < bytes.length; p += 5) if (bytes[p + 4] > 4 || bytes[p + 4] === 1 && bytes.subarray(p, p + 4).some(v => v !== 0)) fail('layout_invalid');
  if (await digest(bytes) !== c.data_sha256) fail('digest_mismatch'); return bytes;
}
function common(body) {
  if (!body || body.schema !== 'OWP-WAVE/1.0') fail(); name(body.gateway_id); name(body.device_id); uuid(body.capture_id);
}

export async function scanWaveformFile(file) {
  if (!(file instanceof Blob) || !file.size || file.size > MAX_FILE) fail('payload_too_large');
  // Web Crypto requires contiguous input. The file is hard bounded to 32 MiB+513;
  // only offsets/hashes survive scanning, and uploads slice one original line.
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (bytes[0] === 239 && bytes[1] === 187 && bytes[2] === 191) fail('json_invalid');
  const lines = [], chunks = new Map(); let m, manifestHash, offset = 0, rawBytes = 0;
  for (let end = 0; end <= bytes.length; end++) {
    if (end !== bytes.length && bytes[end] !== 10) continue;
    if (end === bytes.length && offset === end) break;
    const length = end - offset, limit = lines.length ? 262144 : 65536;
    if (lines.length >= 513 || length > limit) fail('payload_too_large');
    if (!length) fail('json_invalid');
    const payload = bytes.subarray(offset, end), body = strictWaveformJSON(payload); common(body);
    rawBytes += length; if (rawBytes > 33554432) fail('payload_too_large');
    const line = {offset, length, sha256: await digest(payload)};
    if (!m) {if (body.kind !== 'manifest') fail('layout_invalid'); manifest(body, length); m = body; manifestHash = line.sha256;}
    else {
      if (body.kind !== 'chunk') fail('layout_invalid'); const data = await chunk(body);
      if (body.gateway_id !== m.gateway_id || body.device_id !== m.device_id || body.capture_id.toLowerCase() !== m.capture_id.toLowerCase() || body.manifest_sha256 !== manifestHash) fail('reference_conflict');
      const s = m.streams.find(s => s.stream_id === body.stream_id); if (!s) fail('reference_conflict');
      const key = body.stream_id + '/' + body.chunk_index, start = body.chunk_index * s.chunk_frames, n = Math.min(s.chunk_frames, s.frame_count - start);
      if (chunks.has(key) || body.chunk_index >= s.chunk_count || n <= 0 || body.sample_start !== start || body.sample_count !== n || data.length !== n * s.frame_bytes) fail('layout_invalid');
      // Encoding-aware finite checks are server assembly checks, not scan_file's grammar.
      chunks.set(key, line);
    }
    lines.push(line); offset = end + 1;
  }
  if (!m || chunks.size !== m.streams.reduce((n, s) => n + s.chunk_count, 0)) fail('layout_invalid');
  for (const s of m.streams) {
    const data = new Uint8Array(s.decoded_bytes); let at = 0;
    for (let i = 0; i < s.chunk_count; i++) {
      const line = chunks.get(s.stream_id + '/' + i), body = strictWaveformJSON(bytes.subarray(line.offset, line.offset + line.length));
      const piece = Uint8Array.from(atob(body.data_base64), c => c.charCodeAt(0)); data.set(piece, at); at += piece.length;
    }
    if (await digest(data) !== s.data_sha256) fail('digest_mismatch');
  }
  return {file_sha256: await digest(bytes), bytes: file.size, line_count: lines.length, final_lf: bytes.at(-1) === 10,
    gateway_id: m.gateway_id, device_id: m.device_id, capture_id: m.capture_id.toLowerCase(), manifest_sha256: manifestHash, source_kind: m.source_kind, lines};
}
