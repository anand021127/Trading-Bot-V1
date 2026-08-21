/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        primary: '#0a0e1a',
        secondary: '#0f1628',
        card: '#141b2d',
        'card-hover': '#1a2235',
        sidebar: '#0d1424',
        profit: '#10b981',
        loss: '#ef4444',
        accent: '#3b82f6',
        warning: '#f59e0b',
        border: '#1e2d45',
      },
      animation: {
        'flash-green': 'flashGreen 0.6s ease-out',
        'flash-red': 'flashRed 0.6s ease-out',
      },
      keyframes: {
        flashGreen: { '0%': { backgroundColor: 'rgba(16,185,129,0.3)' }, '100%': { backgroundColor: 'transparent' } },
        flashRed: { '0%': { backgroundColor: 'rgba(239,68,68,0.3)' }, '100%': { backgroundColor: 'transparent' } },
      },
    },
  },
  plugins: [],
}
