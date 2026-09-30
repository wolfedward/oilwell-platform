// Wire counts are integer JSON tokens; -0 is accepted just as Python json.loads.
// Original octets are never reconstructed from this validation-only object.
export function strictWaveformJSON(bytes) {
  const text = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(bytes);
  if (/[\r\n]/.test(text)) throw Error('json_invalid');
  let position = 0;
  const invalid = () => {throw Error('json_invalid');};
  const space = () => {while (text[position] === ' ' || text[position] === '\t') position++;};
  function string() {
    const start = position++;
    while (position < text.length) {
      const c = text[position++];
      if (c === '"') return JSON.parse(text.slice(start, position));
      if (c === '\\') position++;
    }
    invalid();
  }
  function value(depth) {
    space(); const c = text[position];
    if (c === '"') return string();
    if (c === '{' || c === '[') {
      if (depth >= 16) throw Error('contract_invalid');
      position++; space(); const object = c === '{', end = object ? '}' : ']';
      const result = object ? Object.create(null) : [];
      if (text[position] === end) {position++; return result;}
      while (position < text.length) {
        if (object) {
          if (text[position] !== '"') invalid();
          const key = string(); space(); if (text[position++] !== ':') invalid();
          if (Object.hasOwn(result, key)) throw Error('json_duplicate_key');
          result[key] = value(depth + 1);
        } else result.push(value(depth + 1));
        space(); const separator = text[position++];
        if (separator === end) return result;
        if (separator !== ',') invalid(); space();
      }
      invalid();
    }
    for (const [literal, decoded] of [['true', true], ['false', false], ['null', null]]) {
      if (text.startsWith(literal, position)) {position += literal.length; return decoded;}
    }
    const token = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(text.slice(position));
    if (!token) invalid();
    position += token[0].length;
    // No legal wire field accepts fraction/exponent number tokens.
    if (!/^-?(?:0|[1-9][0-9]*)$/.test(token[0]) || token[0].length > 11) throw Error('contract_invalid');
    return Number(token[0]);
  }
  const result = value(0); space(); if (position !== text.length) invalid();
  return result;
}
