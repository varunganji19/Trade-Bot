import js from '@eslint/js';
import globals from 'globals';

export default [
  {
    files: ['bot/static/app.js'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'script',
      globals: {
        ...globals.browser,
        Chart: 'readonly',
      },
    },
    rules: {
      ...js.configs.recommended.rules,
    },
  },
];
