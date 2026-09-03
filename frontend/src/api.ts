export interface ReconcileRunResponse {
  run_id: number;
  orders_loaded: number;
  settlements_loaded: number;
  bank_txns_loaded: number;
  auto_matched: number;
  needs_review: number;
  unresolved: number;
  duplicate_settlements: number;
  phantom_credits: number;
  runtime_ms: number;
}

export interface MetricsResponse {
  total_processed: number;
  auto_matched: number;
  needs_review: number;
  unresolved: number;
  match_rate_pct: number;
  value_auto_matched: number;
  value_in_exceptions: number;
  avg_runtime_ms: number;
  last_run_id: number | null;
}

export interface DiffEntry {
  field: string;
  expected: any;
  actual: any;
  delta: any;
  signal: string;
  weight: number;
  score: number;
  is_shortfall: boolean;
}

export interface ExceptionSchema {
  order_id: string;
  status: 'NEEDS_REVIEW' | 'UNRESOLVED';
  subtype: string;
  composite_score: number;
  shortfall: number;
  anomaly_flags: string[];
  has_candidate: boolean;
  resolution_hint: string;
  entries: DiffEntry[];
}

export interface ExceptionListResponse {
  total: number;
  items: ExceptionSchema[];
}

export interface ExplainResponse {
  order_id: string;
  explanation: string;
  llm_status: 'ok' | 'cached' | 'fallback';
  raw_diff: any;
  potential_hallucination: boolean;
  latency_ms: number;
}

export interface ChatRequest {
  question: string;
}

export interface ChatResponse {
  question: string;
  answer: string;
  context_used: string;
  llm_status: 'ok' | 'fallback';
}

export interface AuditLogEntrySchema {
  event_type: string;
  order_id: string | null;
  model_name: string;
  prompt_summary: string;
  response_text: string;
  llm_status: string;
  latency_ms: number;
  potential_hallucination: boolean;
  timestamp_utc: string;
}

export interface AuditLogResponse {
  total: number;
  page: number;
  page_size: number;
  items: AuditLogEntrySchema[];
}

// Helper to handle API errors
async function fetchApi<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = 'API Error';
    try {
      const data = await response.json();
      message = data.detail || message;
    } catch (e) {
      // Ignore JSON parse error if response is not JSON
    }
    const error = new Error(message);
    (error as any).status = response.status;
    throw error;
  }
  return response.json();
}

export const api = {
  runReconciliation: () => fetchApi<ReconcileRunResponse>('/api/reconcile/run', { method: 'POST' }),
  getMetrics: () => fetchApi<MetricsResponse>('/api/metrics'),
  getExceptions: () => fetchApi<ExceptionListResponse>('/api/exceptions'),
  explainException: (orderId: string) => fetchApi<ExplainResponse>(`/api/exceptions/${orderId}/explain`),
  chat: (question: string) => fetchApi<ChatResponse>('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question })
  }),
  getAuditLog: (page = 1, eventType?: string) => {
    const params = new URLSearchParams({ page: page.toString() });
    if (eventType) params.append('event_type', eventType);
    return fetchApi<AuditLogResponse>(`/api/audit-log?${params.toString()}`);
  }
};
