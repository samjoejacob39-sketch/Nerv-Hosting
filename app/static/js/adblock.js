/**
 * Anti-Adblock Detection & Modal Enforcement
 *
 * Checks for adblocker interference via DOM bait elements and style checks.
 * Triggers a full-screen blur penalty modal with a strictly enforced 3-second
 * countdown timer before the user can dismiss it.
 */
(function () {
  'use strict';

  var BAIT_CLASSES = 'adsbygoogle ad-banner pub_300x250 pub_300x250m pub_728x90 text-ad textAd text_ad text_ads';

  function createBaitElement() {
    var bait = document.createElement('div');
    bait.className = BAIT_CLASSES;
    bait.setAttribute('aria-hidden', 'true');
    bait.style.position = 'absolute';
    bait.style.left = '-9999px';
    bait.style.top = '-9999px';
    bait.style.width = '1px';
    bait.style.height = '1px';
    bait.style.pointerEvents = 'none';
    bait.innerHTML = '&nbsp;';
    document.body.appendChild(bait);
    return bait;
  }

  function isAdblockActive(bait) {
    if (!bait || (!bait.offsetParent && bait.offsetHeight === 0 && bait.offsetWidth === 0)) {
      return true;
    }
    var style = window.getComputedStyle(bait);
    if (
      style.display === 'none' ||
      style.visibility === 'hidden' ||
      style.opacity === '0' ||
      bait.clientHeight === 0
    ) {
      return true;
    }
    return false;
  }

  function showAdblockModal() {
    var modal = document.getElementById('adblock-modal');
    if (!modal) return;

    modal.classList.remove('hidden');
    document.documentElement.classList.add('overflow-hidden');
    document.body.classList.add('overflow-hidden');

    var contentWrap = document.getElementById('main');
    if (contentWrap) {
      contentWrap.classList.add('filter', 'blur-sm', 'pointer-events-none');
    }

    var dismissBtn = document.getElementById('adblock-dismiss-btn');
    if (!dismissBtn) return;

    dismissBtn.disabled = true;
    var remainingSeconds = 3;

    function updateButtonText() {
      if (remainingSeconds > 0) {
        dismissBtn.textContent = "I don't use an adblocker (" + remainingSeconds + "s)";
        dismissBtn.classList.add('opacity-50', 'cursor-not-allowed');
      } else {
        dismissBtn.textContent = "I don't use an adblocker";
        dismissBtn.disabled = false;
        dismissBtn.classList.remove('opacity-50', 'cursor-not-allowed');
      }
    }

    updateButtonText();

    var countdownInterval = setInterval(function () {
      remainingSeconds -= 1;
      updateButtonText();
      if (remainingSeconds <= 0) {
        clearInterval(countdownInterval);
      }
    }, 1000);

    dismissBtn.addEventListener('click', function () {
      if (dismissBtn.disabled) return;
      modal.classList.add('hidden');
      document.documentElement.classList.remove('overflow-hidden');
      document.body.classList.remove('overflow-hidden');
      if (contentWrap) {
        contentWrap.classList.remove('filter', 'blur-sm', 'pointer-events-none');
      }
    }, { once: true });
  }

  function runDetection() {
    var bait = createBaitElement();

    window.requestAnimationFrame(function () {
      setTimeout(function () {
        if (isAdblockActive(bait)) {
          showAdblockModal();
        }
        if (bait && bait.parentNode) {
          bait.parentNode.removeChild(bait);
        }
      }, 150);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', runDetection);
  } else {
    runDetection();
  }
})();
