// Search tokens. Must match code_tree_exporter/rag_export.py (tokens/fold/compact);
// shared/fixtures/rag-token-vectors.json pins both implementations.

const STOPWORDS = new Set(
  (
    "a an and are as at be by do does for from how in is it of on or the this to what when where which who why with " +
    "ai bi cac cho co cua da den duoc gi hay khi khong la ma mot nao nay nhu nhung o ra roi sao se thi tren trong tu va vao voi"
  ).split(" "),
);
const CJK = /[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ]+/g;
const CAMEL = /(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])/g;
const SPLIT = /[^\p{L}\p{N}]+/u;

/** Lower-case and strip diacritics: "Hủy đơn" -> "huy don". */
export function fold(text: string): string {
  return String(text ?? "")
    .replace(/đ/g, "d")
    .replace(/Đ/g, "D")
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .toLowerCase();
}

export function tokens(text: string): string[] {
  const result: string[] = [];
  for (const word of fold(String(text ?? "").replace(CAMEL, " ")).split(SPLIT)) {
    if (!word) continue;
    let position = 0;
    for (const match of word.matchAll(CJK)) {
      plain(word.slice(position, match.index), result);
      const run = match[0];
      if (run.length === 1) result.push(run);
      else for (let i = 0; i < run.length - 1; i++) result.push(run.slice(i, i + 2));
      position = (match.index ?? 0) + run.length;
    }
    plain(word.slice(position), result);
  }
  return result;
}

function plain(word: string, result: string[]): void {
  if (word.length >= 2 && !STOPWORDS.has(word)) result.push(word);
}

export function compact(text: string): string {
  return fold(text).replace(/[^\p{L}\p{N}]/gu, "");
}
