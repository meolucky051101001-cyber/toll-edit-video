export function ErrorBanner({ message }: { message: string }) {
  if (!message) return null;
  return (
    <div role="alert" className="error-banner">
      {message}
    </div>
  );
}
