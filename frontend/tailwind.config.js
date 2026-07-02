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
      boxShadow: {
        glow: '0 0 0 1px rgba(255, 255, 255, 0.04), 0 18px 60px rgba(0, 0, 0, 0.25)',
      },
    },
  },
  plugins: [],
}
