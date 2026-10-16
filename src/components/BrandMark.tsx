import type { SVGProps } from 'react';

type BrandMarkProps = SVGProps<SVGSVGElement> & { size?: number | string };

export function BrandMark({ size = 24, ...props }: BrandMarkProps) {
  return <svg width={size} height={size} viewBox="230 230 564 564" fill="none" aria-hidden="true" focusable="false" {...props}>
    <path d="M708 272H458C365 272 310 324 310 394C310 461 359 499 443 514L586 540C665 554 713 589 713 648C713 714 659 752 570 752H422" stroke="currentColor" strokeWidth="76" strokeLinecap="round" strokeLinejoin="round"/>
    <circle cx="308" cy="752" r="40" fill="currentColor"/>
  </svg>;
}
