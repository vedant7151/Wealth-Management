import { AnimatePresence, motion } from 'motion/react';
import { useTheme } from '../theme/useTheme';

export function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const isDark = theme === 'dark';

  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={toggleTheme}
      role="switch"
      aria-checked={isDark}
      aria-label={`Switch to ${isDark ? 'light' : 'dark'} mode`}
    >
      <motion.span
        className="theme-toggle-track"
        animate={{ backgroundColor: isDark ? 'var(--color-secondary)' : 'var(--color-muted)' }}
        transition={{ duration: 0.25 }}
      >
        <motion.span
          className="theme-toggle-thumb"
          animate={{ x: isDark ? 22 : 2 }}
          transition={{ type: 'spring', stiffness: 500, damping: 32 }}
        >
          <AnimatePresence mode="wait" initial={false}>
            {isDark ? (
              <motion.svg
                key="moon"
                viewBox="0 0 24 24"
                width="13"
                height="13"
                aria-hidden="true"
                initial={{ opacity: 0, rotate: -80, scale: 0.6 }}
                animate={{ opacity: 1, rotate: 0, scale: 1 }}
                exit={{ opacity: 0, rotate: 80, scale: 0.6 }}
                transition={{ duration: 0.2 }}
              >
                <path
                  fill="currentColor"
                  d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79Z"
                />
              </motion.svg>
            ) : (
              <motion.svg
                key="sun"
                viewBox="0 0 24 24"
                width="13"
                height="13"
                aria-hidden="true"
                initial={{ opacity: 0, rotate: 80, scale: 0.6 }}
                animate={{ opacity: 1, rotate: 0, scale: 1 }}
                exit={{ opacity: 0, rotate: -80, scale: 0.6 }}
                transition={{ duration: 0.2 }}
              >
                <circle cx="12" cy="12" r="4.5" fill="currentColor" />
                <g stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <line x1="12" y1="1.5" x2="12" y2="4" />
                  <line x1="12" y1="20" x2="12" y2="22.5" />
                  <line x1="4.22" y1="4.22" x2="5.94" y2="5.94" />
                  <line x1="18.06" y1="18.06" x2="19.78" y2="19.78" />
                  <line x1="1.5" y1="12" x2="4" y2="12" />
                  <line x1="20" y1="12" x2="22.5" y2="12" />
                  <line x1="4.22" y1="19.78" x2="5.94" y2="18.06" />
                  <line x1="18.06" y1="5.94" x2="19.78" y2="4.22" />
                </g>
              </motion.svg>
            )}
          </AnimatePresence>
        </motion.span>
      </motion.span>
      <span className="theme-toggle-label">{isDark ? 'Dark' : 'Light'}</span>
    </button>
  );
}
