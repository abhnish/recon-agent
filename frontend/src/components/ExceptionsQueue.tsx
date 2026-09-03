import React, { useState, useEffect } from 'react';
import { api, type ExceptionSchema, type ExplainResponse } from '../api';
import { ChevronDown, ChevronRight, AlertCircle, RefreshCw } from 'lucide-react';

export const ExceptionsQueue: React.FC = () => {
  const [exceptions, setExceptions] = useState<ExceptionSchema[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // orderId -> ExplainResponse
  const [explanations, setExplanations] = useState<Record<string, ExplainResponse>>({});
  const [loadingExp, setLoadingExp] = useState<Record<string, boolean>>({});
  
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());

  useEffect(() => {
    fetchExceptions();
  }, []);

  const fetchExceptions = async () => {
    try {
      setLoading(true);
      const data = await api.getExceptions();
      setExceptions(data.items);
      setError(null);
    } catch (err: any) {
      if (err.status === 409) {
        setError('No reconciliation run found. Please run the pipeline first.');
      } else {
        setError(err.message || 'Failed to load exceptions');
      }
    } finally {
      setLoading(false);
    }
  };

  const toggleRow = async (orderId: string) => {
    const newExpanded = new Set(expandedRows);
    if (newExpanded.has(orderId)) {
      newExpanded.delete(orderId);
      setExpandedRows(newExpanded);
      return;
    }
    
    newExpanded.add(orderId);
    setExpandedRows(newExpanded);
  };

  const explainException = async (orderId: string) => {
    if (explanations[orderId] || loadingExp[orderId]) return;
    
    try {
      setLoadingExp(prev => ({ ...prev, [orderId]: true }));
      const exp = await api.explainException(orderId);
      setExplanations(prev => ({ ...prev, [orderId]: exp }));
    } catch (err: any) {
      // If LLM fails, the backend fallback still returns 200 with raw_diff.
      // 500s or network errors land here.
      console.error(err);
    } finally {
      setLoadingExp(prev => ({ ...prev, [orderId]: false }));
    }
  };

  if (loading) return <div className="text-center py-10">Loading exceptions...</div>;
  if (error) return <div className="text-status-unresolved bg-status-unresolved-bg p-4 rounded-md">{error}</div>;
  if (exceptions.length === 0) return <div className="text-center py-10 text-text-muted">No exceptions found. All clean!</div>;

  return (
    <div className="bg-white rounded-lg border border-border overflow-hidden shadow-sm">
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm text-text">
          <thead className="bg-slate-50 border-b border-border text-text-muted font-medium">
            <tr>
              <th className="px-4 py-3 w-10"></th>
              <th className="px-4 py-3">Order ID</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Subtype</th>
              <th className="px-4 py-3">Score</th>
              <th className="px-4 py-3">Shortfall</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {exceptions.map(exc => (
              <React.Fragment key={exc.order_id}>
                <tr 
                  className={`hover:bg-slate-50 cursor-pointer ${expandedRows.has(exc.order_id) ? 'bg-slate-50' : ''}`}
                  onClick={() => toggleRow(exc.order_id)}
                >
                  <td className="px-4 py-3 text-text-muted">
                    {expandedRows.has(exc.order_id) ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                  </td>
                  <td className="px-4 py-3 font-medium">{exc.order_id}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 text-xs rounded-full font-medium ${
                      exc.status === 'NEEDS_REVIEW' ? 'bg-status-review-bg text-status-review' : 'bg-status-unresolved-bg text-status-unresolved'
                    }`}>
                      {exc.status}
                    </span>
                  </td>
                  <td className="px-4 py-3">{exc.subtype}</td>
                  <td className="px-4 py-3">{exc.composite_score.toFixed(2)}</td>
                  <td className="px-4 py-3 text-status-unresolved">{exc.shortfall < 0 ? exc.shortfall.toFixed(2) : '-'}</td>
                </tr>
                
                {expandedRows.has(exc.order_id) && (
                  <tr>
                    <td colSpan={6} className="px-0 py-0 border-b-2 border-slate-200">
                      <div className="bg-slate-50 p-6 shadow-inner text-sm space-y-4">
                        
                        {/* Diff Table */}
                        <div>
                          <h4 className="font-semibold mb-2">Structured Diff</h4>
                          <div className="bg-white rounded border border-border overflow-hidden">
                            <table className="w-full text-left text-xs">
                              <thead className="bg-slate-100">
                                <tr>
                                  <th className="px-3 py-2">Field</th>
                                  <th className="px-3 py-2">Expected (Order)</th>
                                  <th className="px-3 py-2">Actual (Bank/Gateway)</th>
                                  <th className="px-3 py-2">Delta</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-border">
                                {exc.entries.map((entry, idx) => (
                                  <tr key={idx} className={entry.is_shortfall ? 'bg-red-50' : ''}>
                                    <td className="px-3 py-2 font-medium">{entry.field}</td>
                                    <td className="px-3 py-2">{String(entry.expected)}</td>
                                    <td className="px-3 py-2">{entry.actual !== null ? String(entry.actual) : '-'}</td>
                                    <td className="px-3 py-2 text-status-unresolved font-medium">{entry.delta !== null ? String(entry.delta) : '-'}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </div>

                        {/* LLM Explanation Section */}
                        <div className="pt-2">
                          {!explanations[exc.order_id] && !loadingExp[exc.order_id] && (
                            <button 
                              onClick={() => explainException(exc.order_id)}
                              className="flex items-center px-4 py-2 bg-primary text-white text-xs rounded hover:bg-primary-hover transition-colors"
                            >
                              <AlertCircle className="w-4 h-4 mr-2" />
                              Generate Explanation
                            </button>
                          )}
                          
                          {loadingExp[exc.order_id] && (
                            <div className="flex items-center text-text-muted animate-pulse">
                              <RefreshCw className="w-4 h-4 mr-2 animate-spin" />
                              Analyzing with Gemini...
                            </div>
                          )}

                          {explanations[exc.order_id] && (
                            <div className="bg-white p-4 rounded border border-blue-100 shadow-sm relative">
                              <div className="absolute top-2 right-2 flex space-x-2">
                                <span className={`text-[10px] px-2 py-0.5 rounded-full ${explanations[exc.order_id].llm_status === 'ok' ? 'bg-green-100 text-green-700' : 'bg-amber-100 text-amber-700'}`}>
                                  {explanations[exc.order_id].llm_status}
                                </span>
                                {explanations[exc.order_id].potential_hallucination && (
                                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-red-100 text-red-700" title="Possible hallucination detected">
                                    Flagged
                                  </span>
                                )}
                              </div>
                              <h4 className="font-semibold mb-1 flex items-center text-blue-900">
                                AI Explanation
                              </h4>
                              <p className="text-text leading-relaxed mt-2 whitespace-pre-wrap">
                                {explanations[exc.order_id].explanation || "Fallback triggered. View raw diff above."}
                              </p>
                              
                              <p className="text-[10px] text-text-muted mt-3">
                                Hint: <span className="font-mono">{exc.resolution_hint}</span>
                              </p>
                            </div>
                          )}
                        </div>

                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
