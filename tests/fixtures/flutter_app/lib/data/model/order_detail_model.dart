enum OrderStatus { pending, confirmed, shipping, completed, cancelled }

class OrderDetailModel {
  final String id;
  final OrderStatus status;
  final bool canCancel;
  final String paymentStatus;
  final String deliveryStatus;

  OrderDetailModel({
    required this.id,
    required this.status,
    required this.canCancel,
    required this.paymentStatus,
    required this.deliveryStatus,
  });

  String get statusLabel => status.name;

  factory OrderDetailModel.fromJson(Map<String, dynamic> json) {
    return OrderDetailModel(
      id: json['id'] as String,
      status: OrderStatus.values.byName(json['status'] as String),
      canCancel: json['canCancel'] as bool,
      paymentStatus: json['paymentStatus'] as String,
      deliveryStatus: json['deliveryStatus'] as String,
    );
  }
}
