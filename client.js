var mc = require('minecraft-protocol');
var redis = require('redis');
var yaml = require('js-yaml');
var properties = require('properties');
var fs = require('fs');

var cfg = yaml.safeLoad(fs.readFileSync('config.yml', {encoding: 'utf8'}));

var lang = properties.parse(fs.readFileSync('en_US.lang', {encoding: 'utf8'}), {});

var servercfg = cfg["minecraft"][process.argv[2]];

var host = servercfg["host"], port = servercfg["port"] || 25565;

var channel = host + ":" + port;

if (typeof servercfg["name"] !== "undefined") {
  channel = servercfg["name"];
}

var user = cfg["mc_user"], password = cfg["mc_password"];

var minecraft;
var client = redis.createClient();

function stripColors(txt) {
  return txt.replace(/§[0-9a-f]/g, '');
}

function relay_message(msg) {
  if (!msg) return;
  client.publish("mcrelay:" + channel, msg);
}

function translate_lang(key, data) {
  var repl = lang[key];
  return repl.replace(/\%(\d+)\$s/g, function(match, p1, offset, string) {
    return data[parseInt(p1)-1];
  });
}

function translate_message(msg) {
  var jsonMsg = JSON.parse(msg);
  if (typeof(jsonMsg.translate) === "undefined") {
    return jsonMsg.text;
  } else {
    return translate_lang(jsonMsg.translate, jsonMsg.using);
  }
  return text;
}

function filter(msg) {
  if (stripColors(msg).match(/\[[A-Za-z0-9_]{1,16} -> [A-Za-z0-9_]{1,16}\]/))
    return false;

  if (stripColors(msg).replace(/\s+/g, '') === '')
    return false;

  return true;
}
 
function connect() {
  minecraft = mc.createClient({
    username: user,
    password: password,
    host: host,
    port: port,
  });
  minecraft.on('connect', function() {
    console.info('connected');
  });
  minecraft.on('end', function(reason) {
    setTimeout(connect, 5000);
  });
  minecraft.on('error', function(err) {
    console.info(err);
  });
  minecraft.on(0x03, function(packet) {
    var msg = translate_message(packet.message);
    if (filter(msg) !== false && msg != "") {
      relay_message(msg);
    }
  });
}

connect();
