import { useState, useEffect } from 'react';
import { api, type MetricsResponse } from './api';
import { Dashboard } from './components/Dashboard';
import { ExceptionsQueue } from './components/ExceptionsQueue';
import { ChatPanel } from './components/ChatPanel';
import { AuditTrail } from './components/AuditTrail';
import { LayoutDashboard, AlertCircle, MessageSquare, ShieldCheck } from 'lucide-react';


function App() {
  const [activeTab, setActiveTab] = useState<'dashboard' | 'chat' | 'audit'>('dashboard');
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [isRunLoading, setIsRunLoading] = useState(false);

  useEffect(() => {
    // Try to fetch metrics on initial load in case a run already exists
    fetchMetrics();
  }, []);

  const fetchMetrics = async () => {
    try {
      const data = await api.getMetrics();
      setMetrics(data);
    } catch (err: any) {
      if (err.status !== 409) {
        console.error("Failed to fetch metrics:", err);
      }
    }
  };

  const handleRunReconciliation = async () => {
    setIsRunLoading(true);
    try {
      await api.runReconciliation();
      await fetchMetrics();
      // If we are not on dashboard, maybe stay where we are, but refresh metrics.
    } catch (err) {
      console.error("Failed to run reconciliation:", err);
      alert("Failed to run reconciliation. Check console for details.");
    } finally {
      setIsRunLoading(false);
    }
  };

  const renderContent = () => {
    switch (activeTab) {
      case 'dashboard':
        return (
          <div className="space-y-8 animate-in fade-in duration-300">
            <Dashboard 
              metrics={metrics} 
              onRunReconciliation={handleRunReconciliation} 
              isRunLoading={isRunLoading} 
            />
            {metrics && (
              <div className="space-y-4">
                <h2 className="text-xl font-semibold text-text">Exceptions Queue</h2>
                <ExceptionsQueue />
              </div>
            )}
          </div>
        );
      case 'chat':
        return (
          <div className="max-w-4xl mx-auto animate-in fade-in duration-300">
            <ChatPanel />
          </div>
        );
      case 'audit':
        return (
          <div className="animate-in fade-in duration-300">
            <AuditTrail />
          </div>
        );
    }
  };

  return (
    <div className="min-h-screen flex bg-background">
      {/* Sidebar */}
      <aside className="w-64 bg-slate-900 text-slate-300 flex flex-col shadow-xl z-20">
        <div className="p-6">
          <h1 className="text-2xl font-bold text-white tracking-tight flex items-center">
            <ShieldCheck className="w-6 h-6 mr-2 text-blue-400" />
            ReconAgent
          </h1>
          <p className="text-xs text-slate-500 mt-1 uppercase tracking-wider font-semibold">Finance Controller</p>
        </div>
        
        <nav className="flex-1 px-4 space-y-2 mt-4">
          <button 
            onClick={() => setActiveTab('dashboard')}
            className={`w-full flex items-center px-4 py-3 text-sm font-medium rounded-lg transition-all ${activeTab === 'dashboard' ? 'bg-primary text-white shadow-sm' : 'text-slate-400 hover:bg-slate-800/50 hover:text-white'}`}
          >
            <LayoutDashboard className="w-5 h-5 mr-3" />
            Dashboard
          </button>
          
          <button 
            onClick={() => setActiveTab('chat')}
            className={`w-full flex items-center px-4 py-3 text-sm font-medium rounded-lg transition-all ${activeTab === 'chat' ? 'bg-primary text-white shadow-sm' : 'text-slate-400 hover:bg-slate-800/50 hover:text-white'}`}
          >
            <MessageSquare className="w-5 h-5 mr-3" />
            Q&A Assistant
          </button>

          <button 
            onClick={() => setActiveTab('audit')}
            className={`w-full flex items-center px-4 py-3 text-sm font-medium rounded-lg transition-all ${activeTab === 'audit' ? 'bg-primary text-white shadow-sm' : 'text-slate-400 hover:bg-slate-800/50 hover:text-white'}`}
          >
            <AlertCircle className="w-5 h-5 mr-3" />
            Audit Trail
          </button>
        </nav>

        <div className="p-4 mt-auto">
          <div className="bg-slate-800/50 rounded-lg p-4 text-xs text-slate-400 border border-slate-700/50">
            <p className="font-semibold text-slate-300 mb-1">System Status</p>
            <div className="flex items-center justify-between mt-2">
              <span>Backend API</span>
              <span className="flex items-center text-green-400"><span className="w-2 h-2 rounded-full bg-green-500 mr-1 animate-pulse"></span> Online</span>
            </div>
            <div className="flex items-center justify-between mt-1">
              <span>Gemini LLM</span>
              <span className="flex items-center text-green-400"><span className="w-2 h-2 rounded-full bg-green-500 mr-1 animate-pulse"></span> Ready</span>
            </div>
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 overflow-auto">
        <div className="p-8 max-w-7xl mx-auto">
          {renderContent()}
        </div>
      </main>
    </div>
  );
}

export default App;
