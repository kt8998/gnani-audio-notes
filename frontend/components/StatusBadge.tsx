import { Status } from "@/lib/api";

export const STATUS_LABEL: Record<Status, string> = {
  uploading: "Uploading",
  queued: "Queued",
  preprocessing: "Preparing audio",
  transcribing: "Transcribing",
  summarizing: "Summarizing",
  completed: "Completed",
  failed: "Failed",
};

export default function StatusBadge({ status }: { status: Status }) {
  return <span className={`badge ${status}`}>{STATUS_LABEL[status]}</span>;
}
