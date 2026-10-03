export interface Tag {
  name: string;
  description?: string;
  book_count?: number;
  mention_count?: number;
}
export interface Book {
  id: number;
  canonical_title: string;
  authors: string[];
  publication_year: number | null;
  description: string | null;
  cover_url: string | null;
  openlibrary_id: string;
  tags: Tag[];
  mention_count: number;
  recommendation_count: number;
  independent_recommenders: number;
  thread_count: number;
  all_time_score: number;
  recent_score: number;
  score_details: {
    formula_version?: number;
    independent_contexts: number;
    negative_users?: number;
    positive_threads?: number;
    positive_dates?: number;
    positive_weight?: number;
    negative_weight?: number;
    recent_half_life_days: number;
  };
  timeline?: { month: string; mentions: number; recommenders: number }[];
}
export interface Mention {
  id: number;
  raw_mention: string;
  context_text: string;
  author: string | null;
  recommendation_strength: number;
  sentiment: number;
  hn_created_at: number;
  hn_url: string;
  thread_url: string;
  thread_title: string;
  tags: Tag[];
}
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}
export interface Stats {
  books: number;
  threads: number;
  comments: number;
  recommenders: number;
  last_updated: string | null;
}
export interface Thread {
  id: number;
  title: string;
  author: string;
  score: number;
  stored_comments: number;
  created_at: number;
}

export async function api<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, { signal });
  if (!response.ok)
    throw new Error(
      response.status === 404
        ? "This page could not be found."
        : "The library is unavailable. Please try again.",
    );
  return response.json() as Promise<T>;
}
