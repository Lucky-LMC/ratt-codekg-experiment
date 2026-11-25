// Primary protocol: an extra .only or nested suite cannot move a later test into slot one.
const { Suite } = require(process.env.MOCHA_PATH);
const registration = new WeakMap();
let next = 0;
const add = Suite.prototype.addTest;
const each = Suite.prototype.eachTest;
Suite.prototype.addTest = function(test) {
  registration.set(test, next++);
  return add.call(this, test);
};
Suite.prototype.hasOnly = () => false;
Suite.prototype.eachTest = function(visit) {
  if (!this.root) return each.call(this, visit);
  const tests = [];
  each.call(this, test => tests.push(test));
  tests.sort((a,b) => registration.get(a) - registration.get(b)).forEach(visit);
  return this;
};
