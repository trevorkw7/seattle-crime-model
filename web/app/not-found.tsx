import Link from 'next/link';

export default function NotFound() {
  return (
    <div className="page">
      <div className="center-msg">
        <h1>Page not found</h1>
        <p>
          <Link href="/">Back to the forecast map</Link>
        </p>
      </div>
    </div>
  );
}
