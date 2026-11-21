// A joke, on request (see slop.css): buzzwords on top of the real figures.

const BANNER = '🚀 UNLOCK THE POWER OF NEXT-GEN AI-DRIVEN FREIGHT SYNERGY ✨ SEAMLESSLY LEVERAGE CUTTING-EDGE INSIGHTS 💎 '
  + 'DELVE INTO A RICH TAPESTRY OF GAME-CHANGING LOGISTICS 🔥 ELEVATE YOUR WORKFLOW 🌈 '
  + 'IT\'S NOT JUST A CRM — IT\'S A JOURNEY 💜 ROBUST. SCALABLE. HOLISTIC. 🦄 ';
const REPLIES = [
  'As an AI language model, I am only a decorative button. ✨',
  'Great question! Let\'s delve into it. (Nothing happens.) 🚀',
  'I hope this message finds you well! Certainly! 💎',
  'Please find below: nothing. Do not hesitate to reach out! 🔥',
];

document.documentElement.dataset.theme = 'dark';

const banner = document.createElement('div');
banner.className = 'slop-banner';
banner.setAttribute('aria-hidden', 'true');
const text = document.createElement('span');
text.textContent = BANNER + BANNER;
banner.append(text);
document.querySelector('.stage').prepend(banner);

document.querySelector('.brand span').textContent = '✨ AI-POWERED SYNERGY HUB ✨';
const made = document.createElement('span');
made.className = 'slop-made';
made.textContent = 'Made with 💜 by AI ✨';
document.querySelector('.rail-foot').append(made);

let asked = 0;
const chat = document.createElement('button');
chat.className = 'slop-chat';
chat.textContent = '💬 Ask our AI Copilot ✨';
chat.addEventListener('click', () => alert(REPLIES[asked++ % REPLIES.length]));
document.body.append(chat);

// every page title gets its sparkle; the check keeps the observer from feeding itself
const title = document.getElementById('title');
const sparkle = () => { if (!title.textContent.endsWith('🚀')) title.textContent = `✨ ${title.textContent} 🚀`; };
new MutationObserver(sparkle).observe(title, { childList: true, characterData: true, subtree: true });
sparkle();
