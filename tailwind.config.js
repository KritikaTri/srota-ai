/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./srotaai/web/templates/**/*.html",
    "./srotaai/web/templates/**/*.jinja2",
  ],
  safelist: [
    // Grid layout — responsive breakpoints needed by templates
    { pattern: /^grid-cols-\d+$/, variants: ['sm','md','lg','xl'] },
    { pattern: /^col-span-\d+$/, variants: ['sm','md','lg','xl'] },
    { pattern: /^gap-\d+$/ },
    // Text/bg colors used in Jinja conditionals
    { pattern: /^(text|bg|border)-(red|amber|emerald|blue|slate|green|violet|indigo|orange|yellow|sky)-(50|100|200|300|400|500|600|700|800|900)$/ },
    // Font sizing
    { pattern: /^text-(xs|sm|base|lg|xl|2xl|3xl|4xl)$/ },
    // Spacing
    { pattern: /^[mp][tbrlxy]?-\d+$/ },
    // Flex
    'flex', 'flex-1', 'flex-wrap', 'items-center', 'items-end', 'justify-between', 'justify-end',
    'space-y-3', 'space-y-4', 'space-y-6',
    // Rounding
    { pattern: /^rounded(-lg|-xl|-full|-t|-b)?$/ },
    // Display
    'hidden', 'block', 'inline-flex', 'inline-block', 'inline',
    // Responsive visibility
    { pattern: /^(sm|md|lg|xl):(block|hidden|flex|grid|inline-flex)$/ },
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      colors: {
        ink:   { DEFAULT: '#0F172A', soft: '#1E293B' },
        paper: '#F8FAFC',
      },
    },
  },
};
