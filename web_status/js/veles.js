/*!
 * VELES web status interaction.
 * Copyright 2013 Samsung Electronics
 * Licensed under Samsung Proprietary License.
 */

function renderGraphviz(desc) {
  var result;
  try {
    var raw_result = Viz(desc, "svg", "circo");
    result = $.parseXML(raw_result);
  } catch(e) {
    result = e;
  }
  return result;
}

var updating = false;
var active_workflow_id = null;
var listed_workflows = null;

function updateUI() {
  if (updating) {
    return;
  }
  updating = true;
	console.log("Update started");
	var msg = {
    request: "workflows",
    args: ["name", "master", "slaves", "time", "user", "graph", "description"]
  };
	$.ajax({
		url: "service",
		type: "POST",
		data: JSON.stringify(msg),
		contentType: "application/json; charset=utf-8",
	    async: true,
	    success: function(ret) {
	      console.log("Received response");
	      listed_workflows = ret;
	      var workflows = Object.keys(ret).map(function(key) {
          return { "key": key, "value": ret[key] };
        });
	      workflows.sort(function(a, b) {
	        return a.value.name > b.value.name;
        });
	      if (active_workflow_id == null) {
	        active_workflow_id = workflows[0].key;
	      }
	      workflows.forEach(function(pair) {
	        var workflow = pair.value;
	        var svg = $(renderGraphviz(workflow.graph)).find("svg");
	        listed_workflows[pair.key].svg = svg.clone()
	        svg.attr("class", "media-object pull-left");
	        svg.attr("width", "100").attr("height", 100);
	        items = '<li class="list-group-item media list-item-media'
	        if (active_workflow_id == pair.key) {
	          items += " active";
	        }
	        items += '" id="';
	        items += pair.key;
	        items += '">\n';
	        items += svg.clone().wrap('<div>').parent().html();
	        items += '<div class="media-body graceful-overflow">\n';
	        items += '<h4 class="list-group-item-heading"><a href="#">';
	        items += workflow.name;
	        items += '</a></h4>\n';
	        items += '<a class="view-plots" href="#" >view plots</a><br/>\n';
	        items += '<span class="list-group-item-text">Master: ';
	        items += '<a href="#"><strong>';
	        items += workflow.master;
	        items += '</strong></a><br/>\n';
	        items += '<span class="badge pull-right">';
	        items += Object.keys(workflow.slaves).length;
	        items += '</span>\n';
	        items += 'Slaves: ';
	        for (var skey in workflow.slaves) {
	          items += '<a href="#"><strong>';
	          items += workflow.slaves[skey].host;
	          items += '</strong></a>, ';
	        }
	        items = items.substring(0, items.length - 2);
          items += '<br/>\n';
	        items += 'Time running: <strong>';
	        items += workflow.time;
	        items += '</strong><br/>\n';
	        items += 'Started by: <i class="glyphicon glyphicon-user">';
	        items += '<a href="#"><strong>';
	        items += workflow.user;
	        items += '</strong></a></i></span>\n';
	        items += '</div>\n';
	        items += '</li>\n';
	      });
	      $("#list-loading-indicator").remove()
	      $("#workflow-list").empty().append(items);
	      console.log("Finished update");
	      setTimeout(activateListItem, 0, active_workflow_id);
	      updating = false;
	    }
	});
}

function activateListItem(item_id) {
  active_workflow_id = item_id;
  var workflow = listed_workflows[item_id];
  workflow.svg.attr("class", "detailed-image").attr("width", "600")
      .attr("height", "600");
  details = workflow.svg.clone().wrap('<div>').parent().html();
  details += '<div class="detailed-description">\n';
  details += '<h3 class="media-heading">';
  details += workflow.name;
  details += '</h3>\n';
  details += workflow.description;
  details += 'This workflow is managed by ';
  details += '<i class="glyphicon glyphicon-user"><a href="#"">';
  details += workflow.user;
  details += '</a></i> on <a href="#">';
  details += workflow.master;
  details += "</a> and has ";
  details += Object.keys(workflow.slaves).length;
  details += ' nodes.<br/>\n';
  details += '<div class="panel panel-borderless">\n';
  details += '<div class="panel-heading details-panel-heading">';
  details += 'Actions</div>\n';
  details += '<div class="btn-group btn-group-justified">\n';
  details += '<div class="btn-group">\n';
  details += '<button type="button" class="btn btn-default">View plots';
  details += '</button>\n';
  details += '</div>\n';
  details += '<div class="btn-group">\n';
  details += '<button type="button" class="btn btn-default">Suspend';
  details += '</button>\n';
  details += '</div>\n';
  details += '<div class="btn-group">\n';
  details += '<button type="button" class="btn btn-danger">Cancel</button>\n';
  details += '</div>\n';
  details += '</div>\n';
  details += '</div>\n';
  details += '<div class="panel panel-borderless">\n';
  details += '<div class="panel-heading details-panel-heading">Slaves';
  details += '</div>\n';
  details += '<div class="panel panel-default panel-margin-zero">\n';
  details += '<table class="table table-condensed">\n';
  details += '<thead>\n';
  details += '<tr>\n';
  details += '<th>ID</th>\n';
  details += '<th>Host</th>\n';
  details += '<th class="power">Power</th>\n';
  details += '<th class="center-cell">Status</th>\n';
  details += '<th class="center-cell">Actions</th>\n';
  details += '</tr>\n';
  details += '</thead>\n';
  details += '<tbody>\n';
  for (var skey in workflow.slaves) {
    var slave = workflow.slaves[skey];
    details += '<tr class="';
    switch (slave.status) {
      case "Working":
        details += "success";
        break;
      case "Waiting":
        details += "warning";
        break;
      case "Offline":
        details += "danger";
        break;
      default:
        break;
    }
    details += '">\n';
    details += '<td><div class="slave-id graceful-overflow">';
    details += skey;
    details += '</div></td>\n';
    details += '<td><a href="#">';
    details += slave.host;
    details += '</a></td>\n';
    details += '<td class="power">';
    details += slave.power;
    details += '</td>\n';
    details += '<td class="center-cell">';
    details += slave.status;
    details += '</td>\n';
    details += '<td class="center-cell">\n';
    if (slave.status != 'Offline') {
      details += '<i class="glyphicon glyphicon-pause"><a class="slave-action" href="#">pause</a></i>, <i class="glyphicon glyphicon-remove"><a class="slave-action" href="#">remove</a></i>\n';
    }
    details += '</td>\n';
    details += '</tr>\n';
  }
  details += '</tbody>\n';
  details += '</table>\n';
  details += '</div>\n';
  details += '</div>\n';
  details += '</div>\n';
  $("#details-loading-indicator").remove()
  $('#workflow-details').empty().append(details);
}

$(window).load(function() {
	setInterval(updateUI, 1000);
});