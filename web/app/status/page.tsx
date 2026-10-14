import type { Metadata } from 'next';
import { StatusView } from '@/components/status/StatusView';

export const metadata: Metadata = { title: 'Data & system status' };

export default function StatusPage() {
  return <StatusView />;
}
