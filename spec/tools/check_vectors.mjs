// An independent JavaScript implementation of spec/format.md, used to check the vectors
// that reference.py generates. Run from the repo root:
//
//     node spec/tools/check_vectors.mjs
//
// It's written from the spec, not translated from reference.py, so the two disagree if
// either one (or the spec) is ambiguous. Node 20 or later; no dependencies.

import { createHash } from "node:crypto";
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const VECTORS = join(dirname(fileURLToPath(import.meta.url)), "..", "vectors");
const MAX = Number.MAX_SAFE_INTEGER;

class FormatError extends Error {
  constructor(code, message) {
    super(`${code}: ${message}`);
    this.code = code;
  }
}
const fail = (code, message) => {
  throw new FormatError(code, message);
};

class Vector {
  constructor(values, dtype) {
    this.values = values;
    this.dtype = dtype;
  }
}

// §1 -------------------------------------------------------------------------------

function checkNumber(x, where) {
  if (typeof x !== "number") fail("invalid-value", `${where}: not a number`);
  if (!Number.isFinite(x)) fail("invalid-value", `${where}: not finite`);
  if (Object.is(x, -0)) fail("invalid-value", `${where}: negative zero`);
  if (Number.isInteger(x) && Math.abs(x) > MAX) fail("invalid-value", `${where}: integer above 2^53 - 1`);
}

function checkString(s, where) {
  if (!s.isWellFormed()) fail("invalid-value", `${where}: lone surrogate`);
}

function checkValue(v, where = "$") {
  if (v === null || typeof v === "boolean") return;
  if (typeof v === "number") return checkNumber(v, where);
  if (typeof v === "string") return checkString(v, where);
  if (Array.isArray(v)) return v.forEach((item, i) => checkValue(item, `${where}[${i}]`));
  if (typeof v === "object" && Object.getPrototypeOf(v) === Object.prototype) {
    for (const [k, item] of Object.entries(v)) {
      checkString(k, where);
      if (item === undefined) fail("invalid-value", `${where}.${k}: undefined`); // absent members aren't passed here
      checkValue(item, `${where}.${k}`);
    }
    return;
  }
  fail("invalid-value", `${where}: ${typeof v} isn't a JSON value`);
}

// A strict parser for files (§6.2): rejects duplicate names, and checks values as it goes.
function parseStrict(text) {
  let i = 0;
  const ws = () => {
    while (i < text.length && " \t\n\r".includes(text[i])) i++;
  };
  const NUMBER = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
  function string() {
    i++; // opening quote
    let out = "";
    for (;;) {
      if (i >= text.length) fail("invalid-json", "unterminated string");
      const c = text[i++];
      if (c === '"') break;
      if (c < " ") fail("invalid-json", "control character in string");
      if (c !== "\\") {
        out += c;
        continue;
      }
      const e = text[i++];
      const simple = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" }[e];
      if (simple !== undefined) out += simple;
      else if (e === "u" && /^[0-9a-fA-F]{4}$/.test(text.slice(i, i + 4))) {
        out += String.fromCharCode(parseInt(text.slice(i, i + 4), 16));
        i += 4;
      } else fail("invalid-json", "bad escape");
    }
    checkString(out, "string");
    return out;
  }
  function value(depth) {
    if (depth > 128) fail("invalid-json", "nested too deeply");
    ws();
    const c = text[i];
    if (c === "{") {
      i++;
      const obj = {};
      ws();
      if (text[i] === "}") return i++, obj;
      for (;;) {
        ws();
        if (text[i] !== '"') fail("invalid-json", "expected a member name");
        const k = string();
        ws();
        if (text[i++] !== ":") fail("invalid-json", "expected :");
        if (Object.hasOwn(obj, k)) fail("invalid-json", `duplicate member ${k}`);
        // defineProperty, so a member named __proto__ is an ordinary member.
        Object.defineProperty(obj, k, { value: value(depth + 1), enumerable: true, writable: true, configurable: true });
        ws();
        if (text[i] === ",") i++;
        else if (text[i] === "}") return i++, obj;
        else fail("invalid-json", "expected , or }");
      }
    }
    if (c === "[") {
      i++;
      const arr = [];
      ws();
      if (text[i] === "]") return i++, arr;
      for (;;) {
        arr.push(value(depth + 1));
        ws();
        if (text[i] === ",") i++;
        else if (text[i] === "]") return i++, arr;
        else fail("invalid-json", "expected , or ]");
      }
    }
    if (c === '"') return string();
    for (const [word, v] of [["true", true], ["false", false], ["null", null]]) {
      if (text.startsWith(word, i)) return (i += word.length), v;
    }
    NUMBER.lastIndex = i;
    const m = NUMBER.exec(text);
    if (!m) fail("invalid-json", `unexpected ${JSON.stringify(text.slice(i, i + 10))}`);
    i += m[0].length;
    const x = Number(m[0]);
    checkNumber(x, "number");
    return x;
  }
  const v = value(0);
  ws();
  if (i !== text.length) fail("invalid-json", "content after the value");
  return v;
}

function parseFile(bytes) {
  if (bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) fail("invalid-json", "byte order mark");
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
  } catch {
    fail("invalid-json", "not UTF-8");
  }
  return parseStrict(text);
}

// §2 -------------------------------------------------------------------------------

const sortedNames = (obj) => Object.keys(obj).sort(); // default sort is by UTF-16 code units

function canonical(v) {
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  if (v !== null && typeof v === "object") {
    return `{${sortedNames(v).map((k) => `${JSON.stringify(k)}:${canonical(v[k])}`).join(",")}}`;
  }
  return JSON.stringify(v); // numbers, strings, literals; §1 already excluded the rest
}

const canonicalBytes = (v) => (checkValue(v), Buffer.from(canonical(v), "utf8"));
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

// §3 -------------------------------------------------------------------------------

function makeKey({ attempt, request, label, variant }) {
  if (typeof attempt !== "number" || !Number.isInteger(attempt) || attempt < 0 || attempt > MAX) {
    fail("invalid-key", "attempt must be an integer from 0 to 2^53 - 1");
  }
  const env = { keyFormat: 1, attempt, request };
  for (const [name, part] of [["label", label], ["variant", variant]]) {
    if (part === undefined) continue;
    if (typeof part !== "string" || part === "") fail("invalid-key", `${name} must be a non-empty string`);
    env[name] = part;
  }
  checkValue(env);
  return env;
}

const entryId = (env) => sha256(canonicalBytes(env));

// §4 -------------------------------------------------------------------------------

const NEEDS_ESCAPE = /^\$+llmemo/;
const ESCAPED = /^\$\$+llmemo/;
const RESERVED = /^\$llmemo/;
const HEX64 = /^[0-9a-f]{64}$/;
const REF_KINDS = { "$llmemo.text": "txt", "$llmemo.f64": "f64", "$llmemo.f32": "f32" };

function vectorBytes(vec) {
  if (vec.values.length === 0) fail("invalid-value", "empty vector");
  const size = vec.dtype === "f64" ? 8 : 4;
  const view = new DataView(new ArrayBuffer(vec.values.length * size));
  vec.values.forEach((x, i) => {
    checkNumber(x, "vector");
    if (size === 8) view.setFloat64(i * 8, x, true);
    else {
      if (!Number.isFinite(Math.fround(x))) fail("invalid-value", "outside float32 range");
      view.setFloat32(i * 4, x, true);
    }
  });
  return Buffer.from(view.buffer);
}

function toStored(v, blobs, allowVectors) {
  if (v instanceof Vector) {
    if (!allowVectors) fail("invalid-value", "vector outside the reply");
    const data = vectorBytes(v);
    const h = sha256(data);
    blobs.set(`${h}.${v.dtype}`, data);
    return { [`$llmemo.${v.dtype}`]: h };
  }
  if (typeof v === "string") {
    checkString(v, "string");
    const data = Buffer.from(v, "utf8");
    if (!v.includes("\n") && data.length <= 512) return v;
    const h = sha256(data);
    blobs.set(`${h}.txt`, data);
    return { "$llmemo.text": h };
  }
  if (Array.isArray(v)) return v.map((item) => toStored(item, blobs, allowVectors));
  if (v !== null && typeof v === "object") {
    const out = {};
    for (const [k, item] of Object.entries(v)) {
      Object.defineProperty(out, NEEDS_ESCAPE.test(k) ? "$" + k : k, {
        value: toStored(item, blobs, allowVectors), enumerable: true, writable: true, configurable: true,
      });
    }
    return out;
  }
  checkValue(v);
  return v;
}

function readBlob(root, h, ext, limits) {
  const path = join(root, h.slice(0, 3), `${h}.${ext}`);
  if (!existsSync(path)) fail("blob-missing", path);
  if (statSync(path).size > limits.blobBytes) fail("too-large", path);
  const data = readFileSync(path);
  if (sha256(data) !== h) fail("blob-mismatch", path);
  if (ext === "txt") {
    try {
      return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(data);
    } catch {
      fail("blob-invalid", "text isn't UTF-8");
    }
  }
  const size = ext === "f64" ? 8 : 4;
  if (data.length === 0 || data.length % size) fail("blob-invalid", `vector length ${data.length}`);
  const view = new DataView(data.buffer, data.byteOffset, data.length);
  const values = [];
  for (let off = 0; off < data.length; off += size) {
    const x = size === 8 ? view.getFloat64(off, true) : view.getFloat32(off, true);
    if (!Number.isFinite(x)) fail("blob-invalid", "vector value isn't finite");
    values.push(x);
  }
  return values;
}

// root === null checks only the syntax.
function fromStored(v, where, root, limits) {
  if (Array.isArray(v)) return v.map((item) => fromStored(item, where, root, limits));
  if (v === null || typeof v !== "object") return v;
  const names = Object.keys(v);
  const reserved = names.filter((k) => RESERVED.test(k));
  if (reserved.length) {
    const name = reserved[0];
    if (names.length !== 1 || !(name in REF_KINDS)) fail("schema", `malformed reference ${names}`);
    const ext = REF_KINDS[name];
    if (ext !== "txt" && where !== "reply") fail("schema", `vector reference in ${where}`);
    const h = v[name];
    if (typeof h !== "string" || !HEX64.test(h)) fail("schema", `bad reference ${h}`);
    return root === null ? v : readBlob(root, h, ext, limits);
  }
  const out = {};
  for (const k of names) {
    Object.defineProperty(out, ESCAPED.test(k) ? k.slice(1) : k, {
      value: fromStored(v[k], where, root, limits), enumerable: true, writable: true, configurable: true,
    });
  }
  return out;
}

// §6 -------------------------------------------------------------------------------

function pretty(v, indent = "") {
  const inner = indent + "  ";
  if (Array.isArray(v)) {
    if (!v.length) return "[]";
    return `[\n${v.map((item) => inner + pretty(item, inner)).join(",\n")}\n${indent}]`;
  }
  if (v !== null && typeof v === "object") {
    const names = sortedNames(v);
    if (!names.length) return "{}";
    return `{\n${names.map((k) => `${inner}${JSON.stringify(k)}: ${pretty(v[k], inner)}`).join(",\n")}\n${indent}}`;
  }
  return canonical(v);
}

const jsonFile = (v) => (checkValue(v), Buffer.from(pretty(v) + "\n", "utf8"));

const GITATTRIBUTES =
  "# Written by llmemo. Stored text keeps its exact bytes.\n*.txt -text\n*.f32 binary\n*.f64 binary\n*.json text eol=lf\n";
const GITIGNORE = "# Written by llmemo.\n*.lock\n*.tmp\n";

// The files a writer produces for one entry, as a map of relative path to bytes.
function writeTree(env, meta, { reply, error, failureId }) {
  const files = new Map([
    ["llmemo.json", jsonFile({ layoutFormat: 1 })],
    [".gitattributes", Buffer.from(GITATTRIBUTES)],
    [".gitignore", Buffer.from(GITIGNORE)],
  ]);
  const blobs = new Map();
  const entry = { entryFormat: 1, key: toStored(env, blobs, false), meta };
  const contentName = error !== undefined ? "error" : "reply";
  entry[contentName] = toStored(error !== undefined ? error : reply, blobs, contentName === "reply");
  entry.contentSha256 = sha256(canonicalBytes(entry[contentName]));
  for (const [name, data] of blobs) files.set(`${name.slice(0, 3)}/${name}`, data);
  const id = entryId(env);
  const name = failureId ? `${id}.${failureId}.failure.json` : `${id}.json`;
  files.set(`${id.slice(0, 3)}/${name}`, jsonFile(entry));
  return files;
}

// §5 and §8 ------------------------------------------------------------------------

const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const isNonNegInt = (x) => Number.isInteger(x) && x >= 0;
const isObject = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

function checkMeta(meta) {
  if (!isObject(meta)) fail("schema", "meta");
  if (typeof meta.recordedAt !== "string" || !TIMESTAMP.test(meta.recordedAt)) fail("schema", "meta.recordedAt");
  for (const k of ["recordedBy", "servedModel", "servedBy", "toolVersion", "codeVersion"]) {
    if (k in meta && typeof meta[k] !== "string") fail("schema", `meta.${k}`);
  }
  if ("latencyMs" in meta && !isNonNegInt(meta.latencyMs)) fail("schema", "meta.latencyMs");
  if ("costUsd" in meta && !(typeof meta.costUsd === "number" && meta.costUsd >= 0)) fail("schema", "meta.costUsd");
  if ("usage" in meta) {
    if (!isObject(meta.usage)) fail("schema", "meta.usage");
    for (const k of ["inputTokens", "outputTokens", "reasoningTokens", "cacheReadTokens", "cacheWriteTokens"]) {
      if (k in meta.usage && !isNonNegInt(meta.usage[k])) fail("schema", `meta.usage.${k}`);
    }
  }
  if ("tags" in meta && !(isObject(meta.tags) && Object.values(meta.tags).every((t) => typeof t === "string"))) {
    fail("schema", "meta.tags");
  }
  if ("extra" in meta && !isObject(meta.extra)) fail("schema", "meta.extra");
}

function checkError(error) {
  if (!isObject(error)) fail("schema", "error");
  const names = Object.keys(error);
  const allowed = ["kind", "message", "httpStatus", "exitCode", "details"];
  if (!("kind" in error && "message" in error) || names.some((k) => !allowed.includes(k))) fail("schema", "error members");
  if (typeof error.kind !== "string" || typeof error.message !== "string") fail("schema", "error kind/message");
  for (const k of ["httpStatus", "exitCode"]) if (k in error && !Number.isInteger(error[k])) fail("schema", `error.${k}`);
}

function checkKey(key) {
  if (!isObject(key)) fail("schema", "key");
  const names = Object.keys(key);
  const allowed = ["keyFormat", "attempt", "label", "variant", "request"];
  if (!["keyFormat", "attempt", "request"].every((k) => names.includes(k)) || names.some((k) => !allowed.includes(k))) {
    fail("schema", "key members");
  }
  if (key.keyFormat !== 1) fail("schema", "keyFormat");
}

function readEntryFile(root, path, requested, limits) {
  if (statSync(path).size > limits.entryBytes) fail("too-large", path);
  const entry = parseFile(readFileSync(path));
  if (!isObject(entry)) fail("schema", "entry");
  if (!("entryFormat" in entry)) fail("schema", "no entryFormat");
  if (entry.entryFormat !== 1) fail("unsupported-format", `entryFormat ${entry.entryFormat}`);
  const hasReply = "reply" in entry;
  const hasError = "error" in entry;
  const contentName = hasReply ? "reply" : "error";
  const expected = ["contentSha256", "entryFormat", "key", "meta", contentName].sort();
  if (hasReply === hasError || canonical(Object.keys(entry).sort()) !== canonical(expected)) fail("schema", "entry members");
  if (typeof entry.contentSha256 !== "string" || !HEX64.test(entry.contentSha256)) fail("schema", "contentSha256");
  checkMeta(entry.meta);
  fromStored(entry.key, "key", null, limits);
  fromStored(entry[contentName], contentName, null, limits);
  if (sha256(canonicalBytes(entry[contentName])) !== entry.contentSha256) fail("content-mismatch", path);
  const key = fromStored(entry.key, "key", root, limits);
  checkKey(key);
  if (canonical(key) !== canonical(requested)) fail("key-mismatch", path);
  const content = fromStored(entry[contentName], contentName, root, limits);
  if (hasError) checkError(content);
  return { [contentName]: content, meta: entry.meta };
}

function readEntry(root, requested, limits) {
  const marker = join(root, "llmemo.json");
  if (!existsSync(marker)) fail("not-a-cache", root);
  const info = parseFile(readFileSync(marker));
  if (!isObject(info) || info.layoutFormat !== 1) fail("unsupported-format", "layoutFormat");
  const id = entryId(requested);
  const path = join(root, id.slice(0, 3), `${id}.json`);
  if (!existsSync(path)) return null;
  return readEntryFile(root, path, requested, limits);
}

// Checking the vectors -------------------------------------------------------------

let failures = 0;
let passes = 0;
function check(name, ok, detail = "") {
  if (ok) passes++;
  else {
    failures++;
    console.log(`FAIL ${name} ${detail}`);
  }
}
// Runs one case, so an unexpected error is reported as a failure instead of stopping the run.
function guard(name, f) {
  try {
    f();
  } catch (e) {
    check(name, false, `threw ${e.message}`);
  }
}
function expectCode(name, f, code) {
  try {
    f();
    check(name, false, `accepted; expected ${code}`);
  } catch (e) {
    if (!(e instanceof FormatError)) throw e;
    check(name, e.code === code, `got ${e.code}, expected ${code}`);
  }
}

const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));
const defaultLimits = { entryBytes: 1024 * 1024, blobBytes: 16 * 1024 * 1024 };

function listFiles(root) {
  const out = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) walk(p);
      else out.push(relative(root, p).split("\\").join("/"));
    }
  };
  walk(root);
  return out.sort();
}

function markVectors(v) {
  if (Array.isArray(v)) return v.map(markVectors);
  if (isObject(v)) {
    const names = Object.keys(v);
    if (names.length === 1 && names[0] === "$test.vector") return new Vector(v["$test.vector"].values, v["$test.vector"].dtype);
    return Object.fromEntries(names.map((k) => [k, markVectors(v[k])]));
  }
  return v;
}

for (const c of readJson(join(VECTORS, "keys.json"))) {
  const env = makeKey(c.input);
  check(`keys/${c.name} canonical`, canonical(env) === c.canonical, `\n  got  ${canonical(env)}\n  want ${c.canonical}`);
  check(`keys/${c.name} sha256`, entryId(env) === c.sha256);
}

for (const c of readJson(join(VECTORS, "invalid-keys.json"))) {
  if (c.input) {
    expectCode(`invalid-keys/${c.name}`, () => makeKey(c.input), c.error);
    continue;
  }
  let request;
  try {
    request = JSON.parse(c.requestJson); // JavaScript's parser already rejects NaN and Infinity
  } catch {
    check(`invalid-keys/${c.name}`, true);
    continue;
  }
  expectCode(`invalid-keys/${c.name}`, () => makeKey({ attempt: 0, request }), c.error);
}

for (const name of readdirSync(join(VECTORS, "entries")).sort()) guard(`entries/${name}`, () => {
  const dir = join(VECTORS, "entries", name);
  const c = readJson(join(dir, "case.json"));
  const cache = join(dir, "cache");
  const env = makeKey(c.key);
  check(`entries/${name} id`, entryId(env) === c.id);
  const tree = writeTree(env, c.meta, { reply: markVectors(c.reply), error: c.error, failureId: c.failureId });
  const onDisk = listFiles(cache);
  check(`entries/${name} file list`, canonical(onDisk) === canonical([...tree.keys()].sort()),
    `\n  disk  ${onDisk}\n  wrote ${[...tree.keys()].sort()}`);
  for (const [rel, data] of tree) {
    const path = join(cache, rel);
    check(`entries/${name} bytes of ${rel}`, existsSync(path) && Buffer.compare(readFileSync(path), data) === 0);
  }
  if (c.failureId) {
    check(`entries/${name} lookup misses`, readEntry(cache, env, defaultLimits) === null);
    const path = join(cache, c.id.slice(0, 3), `${c.id}.${c.failureId}.failure.json`);
    const r = readEntryFile(cache, path, env, defaultLimits);
    check(`entries/${name} error`, canonical(r.error) === canonical(c.expectedError));
  } else {
    const r = readEntry(cache, env, defaultLimits);
    if (c.error) check(`entries/${name} error`, canonical(r.error) === canonical(c.expectedError));
    else check(`entries/${name} reply`, canonical(r.reply) === canonical(c.expectedReply));
    check(`entries/${name} meta`, canonical(r.meta) === canonical(c.meta));
  }
});

for (const kind of ["read-ok", "read-errors"]) {
  for (const name of readdirSync(join(VECTORS, kind)).sort()) guard(`${kind}/${name}`, () => {
    const dir = join(VECTORS, kind, name);
    const c = readJson(join(dir, "case.json"));
    const env = makeKey(c.key);
    check(`${kind}/${name} id`, entryId(env) === c.id);
    const limits = { ...defaultLimits, ...c.limits };
    const read = () => readEntry(join(dir, "cache"), env, limits);
    if (kind === "read-errors") expectCode(`${kind}/${name}`, read, c.expectedErrorCode);
    else {
      const r = read();
      check(`${kind}/${name}`, r !== null && canonical(r.reply) === canonical(c.expectedReply));
    }
  });
}

console.log(`${passes} checks passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
