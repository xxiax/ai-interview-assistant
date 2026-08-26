/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // 深色专业配色(设计系统输出: Financial Dashboard 变体 + 微调主色)
        surface: {
          DEFAULT: '#0B0F1A', // 应用底色
          raised: '#111827', // 侧边栏/抬升面
          card: '#151C2C', // 卡片
          hover: '#1D2638', // 悬停
          active: '#232E45' // 激活
        },
        stroke: {
          DEFAULT: '#263045', // 边框
          subtle: '#1C2436' // 更弱的分隔线
        },
        ink: {
          primary: '#F1F5F9', // 主文字
          secondary: '#A7B4C9', // 次文字
          muted: '#6B7A93', // 弱文字
          faint: '#4A556C' // 最弱(时间戳等)
        },
        brand: {
          DEFAULT: '#4F7CFF', // 主操作色
          hover: '#6B92FF',
          dim: '#3A5FD9'
        },
        good: '#22C55E',
        warn: '#F59E0B',
        bad: '#EF4444'
      },
      fontFamily: {
        sans: [
          'Inter',
          '-apple-system',
          'BlinkMacSystemFont',
          'Segoe UI',
          'PingFang SC',
          'Microsoft YaHei',
          'sans-serif'
        ]
      },
      boxShadow: {
        card: '0 1px 2px rgba(0,0,0,0.4), 0 0 0 1px #1C2436',
        pop: '0 12px 32px rgba(0,0,0,0.55)'
      },
      animation: {
        'fade-in': 'fadeIn 200ms ease-out',
        'slide-up': 'slideUp 220ms cubic-bezier(0.16, 1, 0.3, 1)',
        'pulse-dot': 'pulseDot 1.6s ease-in-out infinite'
      },
      keyframes: {
        fadeIn: {
          from: { opacity: '0' },
          to: { opacity: '1' }
        },
        slideUp: {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'translateY(0)' }
        },
        pulseDot: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.35' }
        }
      }
    }
  },
  plugins: []
}
