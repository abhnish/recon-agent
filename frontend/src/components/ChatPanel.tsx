import React, { useState, useRef, useEffect } from 'react';
import { api } from '../api';
import { Send, User, Bot, AlertCircle } from 'lucide-react';

interface Message {
  role: 'user' | 'system';
  content: string;
  contextUsed?: string;
  llmStatus?: string;
  isError?: boolean;
}

export const ChatPanel: React.FC = () => {
  const [messages, setMessages] = useState<Message[]>([
    { role: 'system', content: 'Hello. I can answer questions about the current reconciliation results. What would you like to know?' }
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSend = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || loading) return;

    const userMsg = input.trim();
    setInput('');
    setMessages(prev => [...prev, { role: 'user', content: userMsg }]);
    setLoading(true);

    try {
      const resp = await api.chat(userMsg);
      setMessages(prev => [...prev, { 
        role: 'system', 
        content: resp.answer,
        contextUsed: resp.context_used,
        llmStatus: resp.llm_status
      }]);
    } catch (err: any) {
      setMessages(prev => [...prev, { 
        role: 'system', 
        content: err.status === 409 
          ? "I cannot answer questions until a reconciliation run has been executed. Please run the pipeline first."
          : "An error occurred while communicating with the server.",
        isError: true 
      }]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex flex-col h-[calc(100vh-8rem)] bg-white rounded-lg border border-border shadow-sm overflow-hidden">
      <div className="p-4 border-b border-border bg-white flex justify-between items-center">
        <h2 className="font-semibold text-text">Reconciliation Q&A</h2>
        <span className="text-[10px] bg-slate-100 border border-border font-medium text-text-muted px-2 py-1 rounded-full uppercase tracking-wider">Gemini 2.5 Flash</span>
      </div>
      
      <div className="flex-1 overflow-y-auto p-6 space-y-6 bg-slate-50/50">
        {messages.map((msg, idx) => (
          <div key={idx} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div className={`flex max-w-[80%] ${msg.role === 'user' ? 'flex-row-reverse' : 'flex-row'}`}>
              <div className={`flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center ${msg.role === 'user' ? 'bg-primary text-white ml-3' : 'bg-indigo-100 text-indigo-700 mr-3'}`}>
                {msg.role === 'user' ? <User className="w-4 h-4" /> : <Bot className="w-4 h-4" />}
              </div>
              <div className={`p-4 rounded-2xl ${
                  msg.role === 'user' 
                    ? 'bg-primary text-white rounded-tr-sm shadow-sm' 
                    : msg.isError 
                      ? 'bg-status-unresolved-bg text-status-unresolved border border-status-unresolved/20 rounded-tl-sm'
                      : 'bg-white border border-border shadow-sm rounded-tl-sm'
                }`}>
                <p className={`text-sm whitespace-pre-wrap ${msg.role === 'user' ? 'text-white' : 'text-text'}`}>
                  {msg.content}
                </p>
                {msg.contextUsed && (
                  <div className="mt-2 pt-2 border-t border-slate-100">
                    <p className="text-[10px] text-text-muted">
                      <span className="font-semibold">Context:</span> {msg.contextUsed}
                    </p>
                    {msg.llmStatus !== 'ok' && (
                      <p className="text-[10px] text-amber-600 flex items-center mt-1">
                        <AlertCircle className="w-3 h-3 mr-1" />
                        Status: {msg.llmStatus}
                      </p>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex justify-start">
            <div className="flex flex-row">
              <div className="flex-shrink-0 w-8 h-8 rounded-full bg-indigo-100 text-indigo-700 mr-3 flex items-center justify-center">
                <Bot className="w-4 h-4" />
              </div>
              <div className="p-4 bg-white border border-border shadow-sm rounded-2xl rounded-tl-sm flex items-center space-x-2">
                <div className="w-1.5 h-1.5 bg-indigo-300 rounded-full animate-bounce"></div>
                <div className="w-1.5 h-1.5 bg-indigo-300 rounded-full animate-bounce" style={{ animationDelay: '0.15s' }}></div>
                <div className="w-1.5 h-1.5 bg-indigo-300 rounded-full animate-bounce" style={{ animationDelay: '0.3s' }}></div>
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="p-4 bg-white border-t border-border">
        <form onSubmit={handleSend} className="flex relative">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask about shortfalls, duplicates, or specific order IDs..."
            className="flex-1 bg-white border border-border shadow-sm text-text text-sm rounded-full py-3 pl-5 pr-12 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary transition-all"
            disabled={loading}
          />
          <button 
            type="submit" 
            disabled={!input.trim() || loading}
            className="absolute right-2 top-1.5 p-1.5 bg-primary text-white rounded-full hover:bg-primary-hover disabled:opacity-50 transition-colors"
          >
            <Send className="w-4 h-4" />
          </button>
        </form>
      </div>
    </div>
  );
};
