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
          <thead className="bg-white border-b border-border text-text-muted text-xs uppercase tracking-wider font-semibold">
            <tr>
              <th className="px-6 py-4 w-10"></th>
              <th className="px-6 py-4">Order ID</th>
              <th className="px-6 py-4">Status</th>
              <th className="px-6 py-4">Subtype</th>
              <th className="px-6 py-4">Score</th>
              <th className="px-6 py-4 text-right">Shortfall</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {exceptions.map(exc => (
              <React.Fragment key={exc.order_id}>
                <tr 
                  className={`hover:bg-slate-50 cursor-pointer ${expandedRows.has(exc.order_id) ? 'bg-slate-50' : ''}`}
                  onClick={() => toggleRow(exc.order_id)}
                >
                  <td className="px-6 py-4 text-text-muted">
                    {expandedRows.has(exc.order_id) ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                  </td>
                  <td className="px-6 py-4 font-medium">{exc.order_id}</td>
                  <td className="px-6 py-4">
                    <span className={`px-2.5 py-1 text-[11px] uppercase tracking-wider rounded-full font-bold ${
                      exc.status === 'NEEDS_REVIEW' ? 'bg-status-review-bg text-status-review' : 'bg-status-unresolved-bg text-status-unresolved'
                    }`}>
                      {exc.status.replace('_', ' ')}
                    </span>
                  </td>
                  <td className="px-6 py-4">{exc.subtype}</td>
                  <td className="px-6 py-4 font-mono text-xs">{exc.composite_score.toFixed(2)}</td>
                  <td className="px-6 py-4 text-right font-medium text-status-unresolved">{exc.shortfall < 0 ? exc.shortfall.toFixed(2) : '-'}</td>
                </tr>
                
                {expandedRows.has(exc.order_id) && (
                  <tr>
                    <td colSpan={6} className="px-0 py-0 border-b-2 border-slate-200">
                      <div className="bg-slate-50 p-6 shadow-inner text-sm space-y-4">
                        
                        {/* Diff Table */}
                        <div className="mb-6">
                          <h4 className="font-semibold text-text mb-3">Structured Diff</h4>
                          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                            {exc.entries.map((entry, idx) => (
                              <div key={idx} className={`p-4 rounded-lg border ${entry.is_shortfall && entry.delta !== null ? 'bg-status-unresolved-bg/50 border-status-unresolved/20' : 'bg-white border-border shadow-sm'}`}>
                                <p className="text-[10px] text-text-muted mb-3 font-semibold uppercase tracking-wider">{entry.field}</p>
                                <div className="flex justify-between items-end mt-2">
                                  <div>
                                    <p className="text-[10px] text-text-muted">Expected</p>
                                    <p className="text-sm font-medium">{String(entry.expected)}</p>
                                  </div>
                                  <div className="text-right">
                                    <p className="text-[10px] text-text-muted">Actual</p>
                                    <p className="text-sm font-medium">{entry.actual !== null ? String(entry.actual) : '-'}</p>
                                  </div>
                                </div>
                                {entry.delta !== null && (
                                  <div className="mt-3 pt-2 border-t border-slate-100 flex justify-between items-center">
                                    <span className="text-[10px] text-text-muted">Delta</span>
                                    <span className={`text-xs font-semibold ${entry.is_shortfall ? 'text-status-unresolved' : 'text-text'}`}>
                                      {String(entry.delta)}
                                    </span>
                                  </div>
                                )}
                              </div>
                            ))}
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
                            <div className="bg-indigo-50/40 p-5 rounded-lg border border-indigo-100 shadow-sm relative">
                              <div className="absolute top-4 right-4 flex space-x-2">
                                <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${explanations[exc.order_id].llm_status === 'ok' ? 'bg-indigo-100 text-indigo-700' : 'bg-amber-100 text-amber-700'}`}>
                                  {explanations[exc.order_id].llm_status}
                                </span>
                                {explanations[exc.order_id].potential_hallucination && (
                                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-status-unresolved-bg text-status-unresolved font-medium" title="Possible hallucination detected">
                                    Flagged
                                  </span>
                                )}
                              </div>
                              <h4 className="font-semibold mb-3 flex items-center text-indigo-900">
                                <AlertCircle className="w-4 h-4 mr-2 text-indigo-500" />
                                AI Explanation
                              </h4>
                              <p className="text-slate-700 leading-relaxed text-sm whitespace-pre-wrap">
                                {explanations[exc.order_id].explanation || "Fallback triggered. View raw diff above."}
                              </p>
                              
                              <div className="mt-4 pt-3 border-t border-indigo-100/50">
                                <p className="text-[10px] text-indigo-400 font-medium uppercase tracking-wide">
                                  Resolution Hint: <span className="font-mono bg-indigo-100/50 px-1 py-0.5 rounded text-indigo-600 ml-1">{exc.resolution_hint}</span>
                                </p>
                              </div>
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
