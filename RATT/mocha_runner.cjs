// Execute one registered test from its original file with its enclosing Mocha suites.
const fs = require('node:fs');
const [mochaPath, file, indexText, reportPath, timeoutText] = process.argv.slice(2);
const Mocha = require(mochaPath);
const mocha = new Mocha({ timeout: Number(timeoutText), reporter: function () {} });
const write = result => fs.writeFileSync(reportPath, JSON.stringify(result));

async function main() {
  mocha.addFile(file);
  try {
    await mocha.loadFilesAsync();
  } catch (error) {
    write({ status: 'load_error', error: String(error) });
    return;
  }
  const suite = mocha.suite;
  if (suite.hasOnly()) suite.filterOnly();
  const tests = [];
  suite.eachTest(test => tests.push(test));
  const test = tests[Number(indexText)];
  if (!test) {
    write({ status: 'no_block', count: tests.length });
    return;
  }
  function keepSelected(group) {
    group.tests = group.tests.filter(item => item === test);
    group.suites = group.suites.filter(keepSelected);
    group._onlyTests = [];
    group._onlySuites = [];
    return group.tests.length > 0 || group.suites.length > 0;
  }
  keepSelected(suite);
  const result = { count: tests.length, title: test.fullTitle(), status: 'running' };
  write(result);
  const runner = mocha.run(failures => {
    result.status = failures === 0 && runner.stats.passes === 1 ? 'passed' : 'failed';
    write(result);
    process.exit(0);
  });
  runner.on('fail', (_, error) => { result.error = String(error); result.error_code = error.code || null; });
}

main().catch(error => { console.error(error); process.exit(2); });
