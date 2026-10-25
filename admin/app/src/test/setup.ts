import '@testing-library/jest-dom/vitest';

// jsdom 没有 matchMedia：给需要的组件一个最小实现（固定返回不匹配）
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: function (query: string) {
      return {
        matches: false,
        media: query,
        onchange: null,
        addEventListener: function () {},
        removeEventListener: function () {},
        addListener: function () {},
        removeListener: function () {},
        dispatchEvent: function () { return false; },
      };
    },
  });
}
