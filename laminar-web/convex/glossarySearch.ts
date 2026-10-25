import { GLOSSARY_MD } from "./glossary";

/** The glossary's "## " sections that best match the query, at most 3. Scoring: the whole query as a phrase counts
 *  most, then each word (in the heading x3); one-letter words (a, b, A...) count little, they are everywhere.
 *  Notation is split too: "A2b:+50" also looks for "a2b" and "50". */
export function searchGlossary(query: string) {
  const sections = GLOSSARY_MD.split(/\n(?=## )/).map((text) => ({ title: text.split("\n")[0].replace(/^#+\s*/, ""), text }));
  const q = query.toLowerCase().trim();
  const words = q.split(/[^a-z0-9:+%]+/).filter(Boolean);
  const terms = [...new Set(words.flatMap((w) => [w, ...w.split(/[:+%]+/)]).filter((t) => t.length > 1 || /^[a-d]$/.test(t)))];
  if (!terms.length) return { matches: [], sections: sections.map((x) => x.title) };
  const count = (hay: string, t: string) => hay.split(t).length - 1;
  const scored = sections.map((x) => {
    const body = x.text.toLowerCase(), head = x.title.toLowerCase();
    let score = q.length > 2 ? 10 * count(body, q) : 0;
    for (const t of terms) {
      const w = t.length === 1 ? 0.1 : t.length === 2 ? 0.5 : 1;
      score += w * (count(body, t) + 3 * count(head, t));
    }
    return { ...x, score };
  }).filter((x) => x.score >= 1).sort((a, b) => b.score - a.score).slice(0, 3);
  return scored.length ? { matches: scored.map((x) => ({ section: x.title, text: x.text.slice(0, 4000) })) }
                       : { matches: [], sections: sections.map((x) => x.title) };
}
