import type { Metadata } from 'next';
import { MapExplorer } from '@/components/map/MapExplorer';

export const metadata: Metadata = { title: 'Detailed map' };

export default function ExplorePage() {
  return <MapExplorer />;
}
