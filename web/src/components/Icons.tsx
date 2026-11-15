import type { SVGProps } from "react";

const stroke = { fill: "none", stroke: "currentColor", strokeLinecap: "round", strokeLinejoin: "round" } as const;
type P = SVGProps<SVGSVGElement>;

export const Mark = ({ size = 20, ...p }: P & { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 20 20" aria-hidden="true" fill="currentColor" {...p}>
    <rect x="1" y="1" width="5" height="18" rx="1" />
    <rect x="7.5" y="7" width="5" height="12" rx="1" />
    <rect x="14" y="13" width="5" height="6" rx="1" />
  </svg>
);
export const BackIcon = (p: P) => (
  <svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true" {...stroke} strokeWidth="2" {...p}>
    <path d="M19 12H5M11 6l-6 6 6 6" />
  </svg>
);
export const Chevron = (p: P) => (
  <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" {...stroke} strokeWidth="1.8" {...p}>
    <path d="M6 3l5 5-5 5" />
  </svg>
);
export const Check = ({ size = 18, ...p }: P & { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 18 18" aria-hidden="true" {...stroke} strokeWidth="2" {...p}>
    <path d="M3.5 9.5l3.5 3.5 7.5-8" />
  </svg>
);
export const Shield = (p: P) => (
  <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" {...p}>
    <path d="M8 1.5l5.5 2v4.2c0 3.2-2.2 5.6-5.5 6.800-3.300-1.200-5.500-3.600-5.500-6.800V3.500z" />
    <path d="M5.6 8.2l1.7 1.7 3.200-3.500" strokeLinecap="round" />
  </svg>
);
export const OutIcon = (p: P) => (
  <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" {...stroke} strokeWidth="1.7" {...p}>
    <path d="M6 3H3v10h10v-3M9 3h4v4M13 3L7.500 8.500" />
  </svg>
);
export const TodayIcon = (p: P) => (
  <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true" {...stroke} strokeWidth="1.7" {...p}>
    <rect x="3" y="4.5" width="16" height="14" rx="2" />
    <path d="M3 9h16M7.500 2.500v4M14.500 2.500v4" />
  </svg>
);
export const CardsIcon = (p: P) => (
  <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true" {...stroke} strokeWidth="1.7" {...p}>
    <rect x="2.5" y="5" width="17" height="12" rx="2" />
    <path d="M2.500 9.500h17M6 13.500h3" />
  </svg>
);
export const AsksIcon = (p: P) => (
  <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true" {...stroke} strokeWidth="1.7" {...p}>
    <path d="M7.2 3.5l2 4-1.800 1.400a9 9 0 0 0 5.700 5.700l1.400-1.800 4 2v2.700a1.500 1.500 0 0 1-1.600 1.500C9.600 18.300 3.700 12.400 3 5.100A1.500 1.500 0 0 1 4.500 3.500z" />
  </svg>
);
export const StatementsIcon = (p: P) => (
  <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true" {...stroke} strokeWidth="1.7" {...p}>
    <path d="M5 2.500h8.500L17 6v13.500H5z" />
    <path d="M13 2.500V6.500h4M8 11h6M8 14.500h6" />
  </svg>
);
export const SettingsIcon = (p: P) => (
  <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true" {...stroke} strokeWidth="1.7" {...p}>
    <path d="M4 6.500h8M16 6.500h2M4 15.500h2M10 15.500h8" />
    <circle cx="14" cy="6.500" r="2" />
    <circle cx="8" cy="15.500" r="2" />
  </svg>
);
