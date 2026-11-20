import { statusLabel } from '../lib/jobs';
import './JobStatusBadge.css';

export function StatusBadge({ status }: { status: string }) {
  return <span className={`jobs-status jobs-status-${status}`}>{statusLabel(status)}</span>;
}

export default StatusBadge;
