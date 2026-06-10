import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // MongoDB green palette
        mongo: {
          darkest: '#001E12',
          dark:    '#003D2B',
          mid:     '#00684A',
          light:   '#00A35C',
          bright:  '#00ED64',
          pale:    '#C3F5E0',
        },
        // Severity colors (no blue — neutral white for info)
        critical: '#FF4444',
        warning:  '#FFB800',
        info:     '#E6EDF3',
        success:  '#00ED64',
        // Surface colors — pure black, neutral grays (white borders)
        surface: {
          0: '#000000',
          1: '#0A0A0A',
          2: '#141414',
          3: '#262626',
          4: '#3A3A3A',
        },
      },
      fontFamily: {
        mono:  ['JetBrains Mono', 'Fira Code', 'monospace'],
        sans:  ['Inter', 'system-ui', 'sans-serif'],
      },
      animation: {
        'pulse-slow':  'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'slide-in':    'slideIn 0.3s ease-out',
        'fade-in':     'fadeIn 0.2s ease-out',
        'shimmer':     'shimmer 1.5s ease-in-out infinite',
      },
      keyframes: {
        slideIn: {
          '0%':   { transform: 'translateY(-8px)', opacity: '0' },
          '100%': { transform: 'translateY(0)',    opacity: '1' },
        },
        fadeIn: {
          '0%':   { opacity: '0' },
          '100%': { opacity: '1' },
        },
        shimmer: {
          '0%':   { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0'  },
        },
      },
      boxShadow: {
        'glow-green':    '0 0 20px rgba(0, 237, 100, 0.15)',
        'glow-red':      '0 0 20px rgba(255, 68, 68, 0.2)',
        'glow-yellow':   '0 0 20px rgba(255, 184, 0, 0.15)',
        'card':          '0 4px 24px rgba(0, 0, 0, 0.4)',
      },
    },
  },
  plugins: [],
};

export default config;
