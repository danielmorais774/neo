import { useState, useEffect, useRef } from 'react';
import type { ExecutionPlan, PlanStep } from '../types';
import './PlanTracker.css';

interface PlanTrackerProps {
  plan: ExecutionPlan;
}

function PlanTracker({ plan }: PlanTrackerProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [expandedSubPlans, setExpandedSubPlans] = useState<Set<string>>(new Set());
  const runningRef = useRef<HTMLLIElement | null>(null);

  // Auto-scroll to running step
  useEffect(() => {
    if (runningRef.current) {
      runningRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [plan.steps]);

  const completedCount = plan.steps.filter(s => s.status === 'success' || s.status === 'completed').length;
  const failedCount = plan.steps.filter(s => s.status === 'error' || s.status === 'failed').length;
  const totalSteps = plan.steps.length;
  const progressPercent = totalSteps > 0 ? ((completedCount + failedCount) / totalSteps) * 100 : 0;

  const isComplete = plan.status === 'completed' || plan.status === 'failed';
  const statusIcon = plan.status === 'completed' ? '✅' : plan.status === 'failed' ? '❌' : '📋';

  const getStepIcon = (status?: string) => {
    switch (status) {
      case 'completed':
      case 'success':
        return <span className="step-icon completed">✓</span>;
      case 'failed':
      case 'error':
        return <span className="step-icon failed">✗</span>;
      case 'running':
        return <span className="step-icon running"><span className="pulse-dot" /></span>;
      case 'skipped':
        return <span className="step-icon skipped">⏭</span>;
      default:
        return <span className="step-icon pending" />;
    }
  };

  const getToolBadge = (toolName?: string) => {
    if (!toolName) return null;
    const labels: Record<string, string> = {
      run_command: '⌨️ cmd',
      read_file: '📄 read',
      write_file: '✏️ write',
      list_directory: '📁 list',
      search_files: '🔍 search',
      delete_path: '🗑️ delete',
      create_directory: '📁+ mkdir',
      get_system_info: '💻 info',
    };
    return <span className="tool-badge">{labels[toolName] || toolName}</span>;
  };

  const toggleSubPlan = (planId: string) => {
    setExpandedSubPlans(prev => {
      const next = new Set(prev);
      if (next.has(planId)) next.delete(planId);
      else next.add(planId);
      return next;
    });
  };

  const renderStep = (step: PlanStep, index: number, isLast: boolean) => {
    const isRunning = step.status === 'running';
    const normalizedStatus = step.status === 'success' ? 'completed' : step.status === 'error' ? 'failed' : step.status;

    return (
      <li
        key={step.id || index}
        ref={isRunning ? runningRef : null}
        className={`plan-tracker-step ${normalizedStatus} ${isRunning ? 'active' : ''}`}
      >
        <div className="step-timeline">
          {getStepIcon(step.status)}
          {!isLast && <div className={`step-connector ${normalizedStatus}`} />}
        </div>
        <div className="step-body">
          <div className="step-header-row">
            <span className="step-number">{index + 1}</span>
            <span className="step-desc">{step.description || step.title}</span>
            {getToolBadge(step.tool)}
          </div>
          {step.resultSummary && (step.status === 'success' || step.status === 'completed') && (
            <div className="step-result-preview">{step.resultSummary}</div>
          )}
          {step.error && (step.status === 'error' || step.status === 'failed') && (
            <div className="step-error-preview">{step.error}</div>
          )}
        </div>
      </li>
    );
  };

  const renderSubPlan = (subPlan: ExecutionPlan) => {
    const isExpanded = expandedSubPlans.has(subPlan.id || '');
    const subCompleted = subPlan.steps.filter(s => s.status === 'success' || s.status === 'completed').length;

    return (
      <div key={subPlan.id} className="sub-plan-section">
        <button className="sub-plan-toggle" onClick={() => toggleSubPlan(subPlan.id || '')}>
          <span className={`sub-plan-chevron ${isExpanded ? 'expanded' : ''}`}>▸</span>
          <span className="sub-plan-label">Sub-plano: {subPlan.goal}</span>
          <span className="sub-plan-progress">{subCompleted}/{subPlan.steps.length}</span>
        </button>
        {isExpanded && (
          <ul className="plan-tracker-steps sub-plan-steps">
            {subPlan.steps.map((step, i) => renderStep(step, i, i === subPlan.steps.length - 1))}
          </ul>
        )}
      </div>
    );
  };

  return (
    <div className={`plan-tracker ${isComplete ? 'complete' : 'active'} ${collapsed ? 'collapsed' : ''}`}>
      <div className="plan-tracker-header" onClick={() => setCollapsed(c => !c)}>
        <div className="plan-tracker-title">
          <span className="plan-status-icon">{statusIcon}</span>
          <span className="plan-goal">{plan.goal}</span>
        </div>
        <div className="plan-tracker-meta">
          <span className="plan-progress-text">{completedCount}/{totalSteps}</span>
          <span className={`plan-collapse-icon ${collapsed ? '' : 'expanded'}`}>▸</span>
        </div>
      </div>

      <div className="plan-progress-bar-container">
        <div
          className={`plan-progress-bar ${isComplete ? (plan.status === 'completed' ? 'success' : 'failed') : ''}`}
          style={{ width: `${progressPercent}%` }}
        />
      </div>

      {!collapsed && (
        <div className="plan-tracker-body">
          <ul className="plan-tracker-steps">
            {plan.steps.map((step, i) => renderStep(step, i, i === plan.steps.length - 1 && !(plan.subPlans?.length)))}
          </ul>

          {plan.subPlans && plan.subPlans.map(sp => renderSubPlan(sp))}
        </div>
      )}
    </div>
  );
}

export default PlanTracker;
