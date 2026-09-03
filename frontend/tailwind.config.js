/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        background: 'var(--background)',
        surface: 'var(--surface)',
        border: 'var(--border)',
        text: 'var(--text)',
        'text-muted': 'var(--text-muted)',
        primary: 'var(--primary)',
        'primary-hover': 'var(--primary-hover)',
        
        // Status colors
        'status-match': 'var(--status-match)',
        'status-review': 'var(--status-review)',
        'status-unresolved': 'var(--status-unresolved)',
        
        'status-match-bg': 'var(--status-match-bg)',
        'status-review-bg': 'var(--status-review-bg)',
        'status-unresolved-bg': 'var(--status-unresolved-bg)',
      }
    },
  },
  plugins: [],
}
