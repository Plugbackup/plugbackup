import { loadTokens } from './tokens.mjs';

// Colour variants of the mark. "on-light" is for light backgrounds, "on-dark" for dark ones.
export async function logoVariants() {
  const { color } = await loadTokens();
  const graphite = color.palette.neutral[900];
  const chalk = color.palette.neutral[100];
  return {
    'on-light': { ink: graphite, lit: color.brand, ghost: 0.14 },
    'on-dark': { ink: chalk, lit: color.brand, ghost: 0.16 },
    'mono-black': { ink: '#000000', hollow: true },
    'mono-white': { ink: '#FFFFFF', hollow: true },
  };
}
