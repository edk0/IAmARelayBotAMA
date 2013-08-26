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

function send_chat(msg) {
  console.info(">>> " + msg);
  minecraft.write(0x03, {message: msg});
}

function connect() {
  var id;
  minecraft = mc.createClient({
    username: user,
    password: password,
    host: host,
    port: port,
  });
  minecraft.need_autorun = true;
  minecraft.need_updates = true;
  minecraft.on('connect', function() {
    console.info('connected');
  });
  minecraft.on('end', function(reason) {
    clearInterval(id);
    setTimeout(connect, 5000);
  });
  minecraft.on('error', function(err) {
    console.info(err);
  });
  minecraft.on(0x03, function(packet) {
    if (this.need_autorun) {
      this.need_autorun = false;
      if (typeof servercfg["autorun"] !== "undefined") {
        var i; sp = servercfg["autorun"].split("\n");
        for (i = 0; i < sp.length; i++) {
          if (sp[i] != "") send_chat(sp[i]);
        }
      }
    }
    var msg = translate_message(packet.message);
    console.log("<<< " + msg);
    if (filter(msg) !== false && msg != "") {
      relay_message(msg);
    }
  });
  minecraft.on(0x0d, function(packet) {
    console.log(packet);
    this.position = packet;
    this.write(0x0d, packet);
    if (this.need_updates) {
      this.need_updates = false;
      id = setInterval(update, 50, this);
    }
  });
  function update(mc) {
    mc.position.yaw = (mc.position.yaw + 15.0) % 360;
    mc.write(0x0d, mc.position);
  }
}

connect();
