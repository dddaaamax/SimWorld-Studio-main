"use strict";

const crypto = require("crypto");

const ALLOWED_PRIMITIVES = new Set([
  "spawn_blueprint_actor",
  "spawn_actor",
  "set_actor_transform",
  "delete_actor",
  "take_screenshot",
]);

const MAX_EXPANDED_STEPS = 200;
const XY_BOUNDS = 9500;
const Z_MIN = 0;
const Z_MAX = 50000;

function clamp(num, min, max) {
  return Math.max(min, Math.min(max, num));
}

function toNumber(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function toVector3(value, fallback = [0, 0, 0]) {
  if (!Array.isArray(value) || value.length < 3) return [...fallback];
  return [
    toNumber(value[0], fallback[0]),
    toNumber(value[1], fallback[1]),
    toNumber(value[2], fallback[2]),
  ];
}

function clampLocation(loc) {
  const out = toVector3(loc, [0, 0, 0]);
  out[0] = clamp(out[0], -XY_BOUNDS, XY_BOUNDS);
  out[1] = clamp(out[1], -XY_BOUNDS, XY_BOUNDS);
  out[2] = clamp(out[2], Z_MIN, Z_MAX);
  return out;
}

function sanitizeScale(value) {
  const s = toVector3(value, [1, 1, 1]);
  return [
    clamp(s[0], 0.001, 1000),
    clamp(s[1], 0.001, 1000),
    clamp(s[2], 0.001, 1000),
  ];
}

function sanitizePrimitiveArgs(name, args) {
  const input = args && typeof args === "object" ? { ...args } : {};

  if (name === "spawn_blueprint_actor") {
    if (input.location != null) input.location = clampLocation(input.location);
    if (input.scale != null) input.scale = sanitizeScale(input.scale);
    return input;
  }

  if (name === "spawn_actor") {
    if (input.location != null) input.location = clampLocation(input.location);
    if (input.scale != null) input.scale = sanitizeScale(input.scale);
    return input;
  }

  if (name === "set_actor_transform") {
    if (input.location != null) input.location = clampLocation(input.location);
    if (input.scale != null) input.scale = sanitizeScale(input.scale);
    return input;
  }

  return input;
}

function hasOwn(obj, key) {
  return Boolean(obj && Object.prototype.hasOwnProperty.call(obj, key));
}

function resolvePath(context, pathExpr) {
  if (typeof pathExpr !== "string") return undefined;
  const cleaned = pathExpr.replace(/^\$\./, "").trim();
  if (!cleaned) return context;
  const parts = cleaned.split(".");
  let cur = context;
  for (const part of parts) {
    if (!part) continue;
    if (Array.isArray(cur) && /^\d+$/.test(part)) {
      const idx = Number(part);
      cur = cur[idx];
      continue;
    }
    if (cur && typeof cur === "object" && hasOwn(cur, part)) {
      cur = cur[part];
      continue;
    }
    return undefined;
  }
  return cur;
}

function evalGuard(when, context) {
  if (!when) return true;
  if (typeof when === "boolean") return when;
  if (typeof when === "string") {
    const v = resolvePath(context, when);
    return Boolean(v);
  }
  if (typeof when !== "object") return false;

  if (typeof when.path === "string") {
    const left = resolvePath(context, when.path);
    if (hasOwn(when, "equals")) return left === when.equals;
    if (hasOwn(when, "notEquals")) return left !== when.notEquals;
    if (hasOwn(when, "in") && Array.isArray(when.in)) return when.in.includes(left);
    if (hasOwn(when, "exists")) {
      const exists = left !== undefined && left !== null;
      return Boolean(when.exists) ? exists : !exists;
    }
    return Boolean(left);
  }

  if (Array.isArray(when.all)) {
    return when.all.every((cond) => evalGuard(cond, context));
  }
  if (Array.isArray(when.any)) {
    return when.any.some((cond) => evalGuard(cond, context));
  }

  return false;
}

function resolveArgumentTemplate(template, context) {
  if (template == null) return template;

  if (typeof template === "string") {
    if (template.startsWith("$args.")) return resolvePath(context.args, template.slice(6));
    if (template.startsWith("$defaults.")) return resolvePath(context.defaults, template.slice(10));
    if (template.startsWith("$state.")) return resolvePath(context.state, template.slice(7));
    if (template.startsWith("$const.")) return resolvePath(context.consts, template.slice(7));
    if (template.startsWith("$env.")) return resolvePath(context.env, template.slice(5));
    if (template === "$index") return context.index;
    if (template === "$index1") return context.index + 1;
    return template;
  }

  if (Array.isArray(template)) {
    return template.map((item) => resolveArgumentTemplate(item, context));
  }

  if (typeof template === "object") {
    if (typeof template.ref === "string") {
      return resolveArgumentTemplate(template.ref, context);
    }

    if (template.op === "coalesce" && Array.isArray(template.values)) {
      for (const v of template.values) {
        const out = resolveArgumentTemplate(v, context);
        if (out !== undefined && out !== null) return out;
      }
      return null;
    }

    if (template.op === "add" && Array.isArray(template.values)) {
      let sum = 0;
      for (const v of template.values) {
        sum += toNumber(resolveArgumentTemplate(v, context), 0);
      }
      return sum;
    }

    if (template.op === "mul" && Array.isArray(template.values)) {
      let prod = 1;
      for (const v of template.values) {
        prod *= toNumber(resolveArgumentTemplate(v, context), 1);
      }
      return prod;
    }

    if (template.op === "vec_add" && Array.isArray(template.values) && template.values.length >= 2) {
      const base = toVector3(resolveArgumentTemplate(template.values[0], context), [0, 0, 0]);
      for (let i = 1; i < template.values.length; i += 1) {
        const v = toVector3(resolveArgumentTemplate(template.values[i], context), [0, 0, 0]);
        base[0] += v[0];
        base[1] += v[1];
        base[2] += v[2];
      }
      return base;
    }

    if (template.op === "vec_scale") {
      const vec = toVector3(resolveArgumentTemplate(template.value, context), [0, 0, 0]);
      const scale = toNumber(resolveArgumentTemplate(template.factor, context), 1);
      return [vec[0] * scale, vec[1] * scale, vec[2] * scale];
    }

    if (template.op === "concat" && Array.isArray(template.values)) {
      return template.values
        .map((v) => resolveArgumentTemplate(v, context))
        .map((v) => (v == null ? "" : String(v)))
        .join("");
    }

    const out = {};
    for (const [k, v] of Object.entries(template)) {
      out[k] = resolveArgumentTemplate(v, context);
    }
    return out;
  }

  return template;
}

function validateArgumentsTemplate(value, path, errors) {
  const loc = path || "argumentsTemplate";
  if (value == null) return;
  const t = typeof value;

  if (t === "string") {
    const isRefPattern = /^\$(args|defaults|state|const|env)\.[A-Za-z0-9_.]+$/;
    const isRef = isRefPattern.test(value) || value === "$index" || value === "$index1";
    const startsLikeRef = value.startsWith("$args.")
      || value.startsWith("$defaults.")
      || value.startsWith("$state.")
      || value.startsWith("$const.")
      || value.startsWith("$env.");
    if (startsLikeRef && !isRef) {
      errors.push(`${loc}:invalid_reference_expression`);
      return;
    }
    if (value.startsWith("$") && !isRef) {
      errors.push(`${loc}:unsupported_reference`);
    }
    return;
  }

  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i += 1) {
      validateArgumentsTemplate(value[i], `${loc}[${i}]`, errors);
    }
    return;
  }

  if (t === "object") {
    if (hasOwn(value, "$op")) {
      errors.push(`${loc}:operator_key_must_be_op`);
      return;
    }
    if (typeof value.ref === "string") {
      validateArgumentsTemplate(value.ref, `${loc}.ref`, errors);
      return;
    }

    if (value.op) {
      const op = String(value.op);
      const allowed = new Set(["coalesce", "add", "mul", "vec_add", "vec_scale", "concat"]);
      if (!allowed.has(op)) {
        errors.push(`${loc}:unsupported_op:${op}`);
        return;
      }
    }

    for (const [k, v] of Object.entries(value)) {
      if (k === "op") continue;
      validateArgumentsTemplate(v, `${loc}.${k}`, errors);
    }
  }
}

function validateVectorTemplate(value, path, errors) {
  const loc = path || "vector";
  if (value == null) {
    errors.push(`${loc}:missing_vector`);
    return;
  }

  if (typeof value === "string") {
    validateArgumentsTemplate(value, loc, errors);
    return;
  }

  if (Array.isArray(value)) {
    if (value.length < 3) {
      errors.push(`${loc}:vector_array_too_short`);
      return;
    }
    for (let i = 0; i < 3; i += 1) {
      validateArgumentsTemplate(value[i], `${loc}[${i}]`, errors);
    }
    return;
  }

  if (typeof value !== "object") {
    errors.push(`${loc}:invalid_vector_template_type`);
    return;
  }

  if (hasOwn(value, "$op")) {
    errors.push(`${loc}:operator_key_must_be_op`);
    return;
  }

  if (typeof value.ref === "string") {
    validateArgumentsTemplate(value.ref, `${loc}.ref`, errors);
    return;
  }

  const op = value.op != null ? String(value.op) : "";
  if (!op) {
    errors.push(`${loc}:vector_object_requires_op_or_ref`);
    return;
  }
  if (op === "vec_add") {
    if (!Array.isArray(value.values) || value.values.length < 2) {
      errors.push(`${loc}:vec_add_requires_values`);
      return;
    }
    for (let i = 0; i < value.values.length; i += 1) {
      validateVectorTemplate(value.values[i], `${loc}.values[${i}]`, errors);
    }
    return;
  }
  if (op === "vec_scale") {
    validateVectorTemplate(value.value, `${loc}.value`, errors);
    validateArgumentsTemplate(value.factor, `${loc}.factor`, errors);
    return;
  }
  if (op === "coalesce") {
    if (!Array.isArray(value.values) || value.values.length === 0) {
      errors.push(`${loc}:coalesce_requires_values`);
      return;
    }
    for (let i = 0; i < value.values.length; i += 1) {
      validateVectorTemplate(value.values[i], `${loc}.values[${i}]`, errors);
    }
    return;
  }
  errors.push(`${loc}:invalid_vector_op:${op}`);
}

function validatePrimitiveTemplateContract(primitive, argsTemplate, stepPath, errors) {
  const loc = `${stepPath}.argumentsTemplate`;
  const args = argsTemplate && typeof argsTemplate === "object" ? argsTemplate : null;
  if (!args) return;

  const requireKey = (key) => {
    if (!hasOwn(args, key)) errors.push(`${loc}:missing_required_arg:${key}`);
  };
  const validateOptionalVector = (key) => {
    if (hasOwn(args, key)) validateVectorTemplate(args[key], `${loc}.${key}`, errors);
  };

  if (primitive === "spawn_actor") {
    requireKey("name");
    requireKey("static_mesh");
    requireKey("location");
    if (hasOwn(args, "location")) validateVectorTemplate(args.location, `${loc}.location`, errors);
    validateOptionalVector("rotation");
    validateOptionalVector("scale");
    return;
  }

  if (primitive === "spawn_blueprint_actor") {
    requireKey("actor_name");
    requireKey("blueprint_id");
    requireKey("location");
    if (hasOwn(args, "location")) validateVectorTemplate(args.location, `${loc}.location`, errors);
    validateOptionalVector("rotation");
    validateOptionalVector("scale");
    return;
  }

  if (primitive === "set_actor_transform") {
    requireKey("name");
    validateOptionalVector("location");
    validateOptionalVector("rotation");
    validateOptionalVector("scale");
    return;
  }

  if (primitive === "delete_actor") {
    requireKey("name");
    return;
  }

  if (primitive === "take_screenshot") {
    if (hasOwn(args, "filename")) {
      validateArgumentsTemplate(args.filename, `${loc}.filename`, errors);
    }
  }
}

function validateParamsSchemaShape(schema, errors) {
  const out = Array.isArray(errors) ? errors : [];
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) {
    out.push("paramsSchema:must_be_object");
    return out;
  }
  if (schema.type !== "object") {
    out.push("paramsSchema:type_must_be_object");
  }
  if (!schema.properties || typeof schema.properties !== "object" || Array.isArray(schema.properties)) {
    out.push("paramsSchema:properties_must_be_object");
  }
  if (schema.required != null) {
    if (!Array.isArray(schema.required)) {
      out.push("paramsSchema:required_must_be_array");
    } else {
      for (let i = 0; i < schema.required.length; i += 1) {
        const value = schema.required[i];
        if (typeof value !== "string" || !value.trim()) {
          out.push(`paramsSchema:required_invalid_at_${i}`);
        }
      }
    }
  }
  return out;
}

function validateToolShape(tool) {
  if (!tool || typeof tool !== "object") {
    throw new Error("Tool must be an object");
  }

  if (!tool.id || !tool.mcpName || !Array.isArray(tool.program)) {
    throw new Error("Tool is missing required fields: id, mcpName, program");
  }

  if (tool.mcpName !== `learned__${tool.id}`) {
    throw new Error("mcpName must be 'learned__<tool_id>'");
  }

  if (tool.program.length === 0) {
    throw new Error("program_empty");
  }
  if (tool.program.length > MAX_EXPANDED_STEPS) {
    throw new Error(`program_too_long:${tool.program.length}`);
  }

  const errors = [];
  validateParamsSchemaShape(tool.paramsSchema, errors);
  for (let i = 0; i < tool.program.length; i += 1) {
    const step = tool.program[i];
    const stepPath = `program[${i}]`;
    if (!step || typeof step !== "object") {
      errors.push(`${stepPath}:step_must_be_object`);
      continue;
    }
    const primitive = String(step.primitive || "").trim();
    if (!primitive) {
      errors.push(`${stepPath}:missing_primitive`);
    } else if (!ALLOWED_PRIMITIVES.has(primitive)) {
      errors.push(`${stepPath}:primitive_forbidden:${primitive}`);
    }
    if (!step.argumentsTemplate || typeof step.argumentsTemplate !== "object") {
      errors.push(`${stepPath}:missing_argumentsTemplate`);
    } else {
      validateArgumentsTemplate(step.argumentsTemplate, `${stepPath}.argumentsTemplate`, errors);
      validatePrimitiveTemplateContract(primitive, step.argumentsTemplate, stepPath, errors);
    }
    if (step.when != null) {
      const wt = typeof step.when;
      const okWhen = wt === "boolean" || wt === "string" || wt === "object";
      if (!okWhen) errors.push(`${stepPath}:invalid_when_type`);
    }
  }

  if (errors.length > 0) {
    throw new Error(`program_invalid:${errors.join(";")}`);
  }
}

function isFiniteVec3(value) {
  return Array.isArray(value)
    && value.length >= 3
    && Number.isFinite(value[0])
    && Number.isFinite(value[1])
    && Number.isFinite(value[2]);
}

function validateExpandedPrimitiveArgs(name, args, path, errors) {
  const loc = path || String(name || "step");
  const input = args && typeof args === "object" ? args : {};

  const requireString = (key) => {
    const value = input[key];
    if (typeof value !== "string" || !value.trim()) {
      errors.push(`${loc}:missing_or_invalid:${key}`);
    }
  };
  const requireVec3 = (key) => {
    if (!isFiniteVec3(input[key])) {
      errors.push(`${loc}:missing_or_invalid_vec3:${key}`);
    }
  };
  const optionalVec3 = (key) => {
    if (input[key] != null && !isFiniteVec3(input[key])) {
      errors.push(`${loc}:invalid_vec3:${key}`);
    }
  };

  if (name === "spawn_actor") {
    requireString("name");
    requireString("static_mesh");
    requireVec3("location");
    optionalVec3("rotation");
    optionalVec3("scale");
    return;
  }

  if (name === "spawn_blueprint_actor") {
    requireString("actor_name");
    requireString("blueprint_id");
    requireVec3("location");
    optionalVec3("rotation");
    optionalVec3("scale");
    return;
  }

  if (name === "set_actor_transform") {
    requireString("name");
    optionalVec3("location");
    optionalVec3("rotation");
    optionalVec3("scale");
    return;
  }

  if (name === "delete_actor") {
    requireString("name");
    return;
  }

  if (name === "take_screenshot") {
    if (input.filename != null && typeof input.filename !== "string") {
      errors.push(`${loc}:invalid_type:filename`);
    }
  }
}

function expandLearnedTool(tool, callArgs) {
  validateToolShape(tool);

  const defaults = tool.defaults && typeof tool.defaults === "object" ? tool.defaults : {};
  const args = {
    ...defaults,
    ...((callArgs && typeof callArgs === "object") ? callArgs : {}),
  };

  const repeat = clamp(Math.floor(toNumber(args.repeat, defaults.repeat || 1)), 1, MAX_EXPANDED_STEPS);

  const steps = [];
  for (let idx = 0; idx < repeat; idx += 1) {
    for (const progStep of tool.program) {
      const context = {
        args,
        defaults,
        state: tool.state || {},
        consts: tool.consts || {},
        env: { maxSteps: MAX_EXPANDED_STEPS },
        index: idx,
      };

      if (!evalGuard(progStep.when, context)) continue;

      const primitive = String(progStep.primitive);
      const resolvedArgs = resolveArgumentTemplate(progStep.argumentsTemplate || {}, context);
      const contractErrors = [];
      validateExpandedPrimitiveArgs(primitive, resolvedArgs, `expanded.${primitive}`, contractErrors);
      if (contractErrors.length > 0) {
        throw new Error(`expanded_args_invalid:${contractErrors.join(";")}`);
      }
      const sanitized = sanitizePrimitiveArgs(primitive, resolvedArgs);

      steps.push({
        name: primitive,
        arguments: sanitized,
      });

      if (steps.length > MAX_EXPANDED_STEPS) {
        throw new Error(`Expanded step count ${steps.length} exceeds max ${MAX_EXPANDED_STEPS}`);
      }
    }
  }

  if (steps.length === 0) {
    throw new Error("program_expanded_empty");
  }

  return steps;
}

function buildDynamicToolDef(tool) {
  validateToolShape(tool);
  return {
    name: tool.mcpName,
    description: tool.description || `Learned tool ${tool.id}`,
    inputSchema: (tool.paramsSchema && typeof tool.paramsSchema === "object")
      ? tool.paramsSchema
      : { type: "object", properties: {} },
  };
}

function normalizeSignature(tool) {
  const params = Object.keys((tool.paramsSchema && tool.paramsSchema.properties) || {}).sort();
  const primitives = Array.isArray(tool.program)
    ? tool.program.map((step) => String(step && step.primitive || "")).join(",")
    : "";
  return [
    primitives,
    params.join(","),
    tool.defaults && (tool.defaults.blueprint_id || tool.defaults.static_mesh || ""),
  ].join("|");
}

function signatureHash(tool) {
  const sig = normalizeSignature(tool);
  return crypto.createHash("sha256").update(sig).digest("hex");
}

function bigramSet(text) {
  const t = (text || "").toLowerCase().replace(/[^a-z0-9_]+/g, " ").trim();
  if (!t) return new Set();
  const grams = new Set();
  const padded = ` ${t} `;
  for (let i = 0; i < padded.length - 1; i += 1) {
    grams.add(padded.slice(i, i + 2));
  }
  return grams;
}

function similarityScore(a, b) {
  const A = bigramSet(a);
  const B = bigramSet(b);
  if (A.size === 0 && B.size === 0) return 1;
  if (A.size === 0 || B.size === 0) return 0;
  let inter = 0;
  for (const g of A) {
    if (B.has(g)) inter += 1;
  }
  return inter / (A.size + B.size - inter);
}

function dryRunExpansion(tool, sampleArgs) {
  const steps = expandLearnedTool(tool, sampleArgs || tool.defaults || {});
  const errors = [];
  for (const step of steps) {
    validateExpandedPrimitiveArgs(step.name, step.arguments || {}, `dry_run.${step.name}`, errors);
  }
  if (errors.length > 0) {
    throw new Error(`dry_run_contract_invalid:${errors.join(";")}`);
  }
  return { ok: true, stepCount: steps.length };
}

module.exports = {
  ALLOWED_PRIMITIVES,
  MAX_EXPANDED_STEPS,
  buildDynamicToolDef,
  expandLearnedTool,
  signatureHash,
  normalizeSignature,
  similarityScore,
  dryRunExpansion,
  validateParamsSchemaShape,
  validatePrimitiveTemplateContract,
  validateExpandedPrimitiveArgs,
  validateToolShape,
};
