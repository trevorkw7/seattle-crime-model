import type { Metadata } from 'next';
import { EvaluationView } from '@/components/eval/EvaluationView';

export const metadata: Metadata = { title: 'Model evaluation' };

export default function EvaluationPage() {
  return <EvaluationView />;
}
