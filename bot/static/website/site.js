'use strict';

async function post(path, body) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Please try again.');
  return result;
}

const signup = document.querySelector('#signup-form');
if (signup) signup.addEventListener('submit', async event => {
  event.preventDefault();
  const button = signup.querySelector('button'), status = document.querySelector('#signup-status');
  button.disabled = true;
  status.textContent = 'Saving your interest…';
  try {
    const result = await post('/api/waitlist', {email: document.querySelector('#email').value, consent: document.querySelector('#consent').checked});
    document.querySelector('#removal-link').value = new URL('/remove#' + result.removal_token, window.location.origin).href;
    signup.reset();
    signup.hidden = true;
    document.querySelector('#signup-success').hidden = false;
    status.textContent = '';
  } catch (error) {
    status.textContent = error.message;
    button.disabled = false;
  }
});

const remove = document.querySelector('#remove-signup');
if (remove) {
  const token = window.location.hash.slice(1), status = document.querySelector('#remove-status');
  window.history.replaceState(null, '', window.location.pathname);
  if (/^[A-Za-z0-9_-]{32,128}$/.test(token)) remove.disabled = false;
  else status.textContent = 'Open the complete private deletion link you saved when you signed up.';
  remove.addEventListener('click', async () => {
    remove.disabled = true;
    try {
      await post('/api/waitlist/remove', {token});
      status.textContent = 'This signup has been removed, if it was still active.';
    } catch (error) {
      status.textContent = error.message;
      remove.disabled = false;
    }
  });
}
