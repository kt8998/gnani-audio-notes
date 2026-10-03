import RecordingList from "@/components/RecordingList";
import UploadCard from "@/components/UploadCard";

export default function Home() {
  return (
    <>
      <h1>Audio Notes</h1>
      <p className="muted" style={{ marginTop: 0 }}>
        Upload a recording to get a transcript (Gnani speech-to-text) and a summary (Google Gemini).
      </p>
      <UploadCard />
      <RecordingList />
    </>
  );
}
